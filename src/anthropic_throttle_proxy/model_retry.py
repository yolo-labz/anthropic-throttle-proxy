"""Model-scoped subscription pauses and bounded early-reset verification."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping

from . import config, limiter

RECHECK_INTERVAL_S = 300.0
_versions: dict[str, int] = {}
_last_checks: dict[tuple[str, str], float] = {}
_inflight: dict[tuple[str, str], asyncio.Event] = {}


def fable_window(meta: Mapping[str, str] | None, model: str) -> bool:
    """Only explicit scoped rejection plus measured whole-week room narrows a hold."""
    if not meta or "fable" not in model.lower().replace("_", "-").split("-"):
        return False
    prefix = "anthropic-ratelimit-unified-"
    try:
        weekly = float(meta.get(prefix + "7d-utilization", "nan"))
    except (TypeError, ValueError):
        return False
    if meta.get(prefix + "5h-status") not in (None, "allowed", "allowed_warning"):
        return False
    try:
        five_hour = float(meta.get(prefix + "5h-utilization", "0"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(five_hour) or not 0 <= five_hour < 1:
        return False
    return (
        meta.get(prefix + "7d_oi-status") == "rejected"
        and meta.get(prefix + "7d-status") in ("allowed", "allowed_warning")
        and math.isfinite(weekly)
        and 0 <= weekly < 1
    )


def key(bid: str, model: str) -> str:
    # Fable is the only provider-scoped bucket currently supported by evidence.
    return bid + ":model:fable" if "fable" in model.lower().replace("_", "-").split("-") else ""


def scoped_remaining(bid: str, model: str) -> float:
    scope = key(bid, model)
    return (
        max(0.0, limiter._load_retry_after_state().get(scope, 0.0) - time.time()) if scope else 0.0
    )


def note_pause(base, seconds: float, meta: Mapping[str, str] | None, model: str = "") -> bool:
    """One pause recorder; true means this was a proven Fable-only rejection."""
    scoped = fable_window(meta, model)
    target = key(base.bearer_id, model) if scoped else base.bearer_id
    _versions[target] = _versions.get(target, 0) + 1
    if scoped:
        if math.isfinite(seconds) and seconds > 0:
            until = max(limiter._load_retry_after_state().get(target, 0), time.time() + seconds)
            limiter._load_retry_after_state()[target] = until
            limiter._persist_retry_after_state()
            config.log(
                f"model-quota-pause bid={base.bearer_id} model=fable "
                f"remaining={int(until - time.time())}s"
            )
    else:
        until = base.note_retry_after(seconds)
        if not config.RETRY_AFTER_STATE_FILE and until > time.time():
            limiter._load_retry_after_state()[target] = until
    return scoped


def _message_matches(body: bytes, model: str) -> bool:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("type") == "message"
        and value.get("role") == "assistant"
        and value.get("model") == model
        and value.get("stop_reason") in ("end_turn", "max_tokens")
        and isinstance(value.get("content"), list)
        and any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
            for block in value["content"]
        )
    )


async def recheck(
    base,
    model: str,
    send: Callable[[], Awaitable[tuple[int, bytes, Mapping[str, str]]]],
) -> Mapping[str, str] | None:
    """One exact-model probe per account/cadence, never a fan-out of client turns.

    Success clears only the unchanged pause snapshot. Shared whole-account
    recovery retains the existing half-open normal-message gate.
    """
    bid = base.bearer_id
    identity = (bid, model)
    scope = key(bid, model)
    state = limiter._load_retry_after_state()
    targets = {
        target: state[target]
        for target in (bid, scope)
        if target in state
        and (target != bid or base.retry_after_remaining() > config.MAX_HOLD_RETRY_AFTER_S)
    }
    if not targets or max(targets.values()) - time.time() <= config.MAX_HOLD_RETRY_AFTER_S:
        return None
    if identity in _inflight:
        await _inflight[identity].wait()
        return None
    if identity in _last_checks and time.time() - _last_checks[identity] < RECHECK_INTERVAL_S:
        return None
    versions = {target: _versions.get(target, 0) for target in targets}
    _inflight[identity] = asyncio.Event()
    _last_checks[identity] = time.time()
    try:
        status, body, headers = await send()
        if status != 200 or not _message_matches(body, model):
            return None
        # The same-model success cannot erase a rejection that arrived later.
        if any(
            state.get(target) != until or _versions.get(target, 0) != versions[target]
            for target, until in targets.items()
        ):
            return None
        for target in targets:
            if target == bid:
                limiter.clear_retry_after(bid)
            else:
                state.pop(target, None)
                limiter._persist_retry_after_state()
        config.log(f"subscription-early-reset bid={bid} model={model} result=verified")
        return headers
    except (OSError, ValueError):
        return None
    finally:
        _inflight.pop(identity).set()
