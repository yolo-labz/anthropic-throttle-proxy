"""Stop-loss for pi compaction summaries that the client keeps cutting.

pi gives every request a client deadline of about 300 s, queue wait included
(the 1.4 cores, and the 1.5.2 cores measured on 13/11/2026). A compaction
summary of a large context at high effort can need longer. pi then sends the
summary again, on a larger context, before every later model call, so the
session loops: each model call first pays about five minutes and one uncached
read of the whole history for a summary that is cut again. The gateway usage
ledger for 09/11 11:47 to 13/11 07:58 holds 2,834 cut summaries (298.8M
uncached input tokens). After a cut, the session's next summary within 30 min
was cut again in 80.3 % of cases and completed in 1.1 %.

Policy: when a summary is cut by the client (499) after at least
``SUMMARY_BREAKER_MIN_S`` of upstream time, remember its session for
``SUMMARY_BREAKER_TTL_S``. While remembered, the session's summaries get a
non-retryable 400 from the gateway, before any queue, limiter or upstream
work. pi reports the failed compaction and continues the turn on the
uncompacted context, as it already does after the cut, without the wait or
the tokens. A summary that completes clears the mark.

A pi summary is recognised by pi's summarization system prompt. The session
key is ``session_affinity.derive_key`` (the hash the usage ledger stores).
Logs carry only that hash's prefix and numbers, never body content.

ponytail: in-process, bounded LRU with a TTL; a restart forgets the marks, and
each looping session then pays one more cut summary. Upgrade path: persist the
marks if restarts become frequent.
"""

from __future__ import annotations

import json
import math
import time
from collections import OrderedDict

from aiohttp import web
from prometheus_client import Counter

from . import config, session_affinity
from .config import log
from .metrics import REGISTRY

_MARKER = "You are a context summarization assistant."
_MARKER_BYTES = _MARKER.encode()

session_key = web.RequestKey("summary_breaker_session", str)

# key -> (monotonic time of the cut, upstream seconds before the cut). Ordered
# by arm time (re-arming moves the key to the end), so expiry prunes the front.
_armed: OrderedDict[str, tuple[float, float]] = OrderedDict()

_events = Counter(
    "throttle_summary_breaker_total",
    "pi summary stop-loss events: armed (a summary was cut late), refused, "
    "observed (observe mode, would refuse) and cleared (a summary completed).",
    ["action"],
    registry=REGISTRY,
)


def is_pi_summary(body: bytes | None) -> bool:
    """True when ``body`` is a pi compaction summary (pi's summarization system prompt)."""
    if not body or _MARKER_BYTES not in body:
        return False
    try:
        obj = json.loads(body)
    except ValueError:
        return False
    system = obj.get("system") if isinstance(obj, dict) else None
    if isinstance(system, str):
        return system.startswith(_MARKER)
    if not isinstance(system, list):
        return False
    return any(
        isinstance(block, dict)
        and isinstance(block.get("text"), str)
        and block["text"].startswith(_MARKER)
        for block in system
    )


def gate(request: web.Request, body: bytes | None, model: str) -> web.Response | None:
    """Tag a pi summary with its session key; refuse it while that session is armed."""
    mode = config.SUMMARY_BREAKER_MODE
    if mode == "off" or not is_pi_summary(body):
        return None
    key = session_affinity.derive_key(body, model)
    if not key:
        return None
    request[session_key] = key
    now = time.monotonic()
    entry = _live_entry(key, now)
    if entry is None:
        return None
    armed_at, cut_after_s = entry
    remaining_s = armed_at + config.SUMMARY_BREAKER_TTL_S - now
    action = "refused" if mode == "enforce" else "observed"
    _events.labels(action=action).inc()
    log(
        f"summary-breaker {action} key={key[:12]} cut_after_s={cut_after_s:.0f} "
        f"remaining_s={remaining_s:.0f} mode={mode}"
    )
    return _refusal(remaining_s) if mode == "enforce" else None


def settle(request: web.Request, status: int, upstream_s: float) -> None:
    """After a tagged summary ends: arm on a late client cut, clear on success."""
    key = request.get(session_key)
    if not key:
        return
    if status == 499 and upstream_s >= config.SUMMARY_BREAKER_MIN_S:
        _arm(key, upstream_s, time.monotonic())
        _events.labels(action="armed").inc()
        log(
            f"summary-breaker armed key={key[:12]} cut_after_s={upstream_s:.0f} "
            f"ttl_s={config.SUMMARY_BREAKER_TTL_S:.0f} sessions={len(_armed)}"
        )
    elif 200 <= status < 300 and _armed.pop(key, None) is not None:
        _events.labels(action="cleared").inc()
        log(f"summary-breaker cleared key={key[:12]}")


def armed_sessions() -> int:
    return len(_armed)


def reset_for_tests() -> None:
    _armed.clear()


def _arm(key: str, cut_after_s: float, now: float) -> None:
    _prune(now)
    _armed[key] = (now, cut_after_s)
    _armed.move_to_end(key)
    while len(_armed) > config.SUMMARY_BREAKER_MAX_SESSIONS:
        _armed.popitem(last=False)


def _prune(now: float) -> None:
    while _armed:
        key, (armed_at, _cut) = next(iter(_armed.items()))
        if now - armed_at < config.SUMMARY_BREAKER_TTL_S:
            return
        del _armed[key]


def _live_entry(key: str, now: float) -> tuple[float, float] | None:
    entry = _armed.get(key)
    if entry is not None and now - entry[0] >= config.SUMMARY_BREAKER_TTL_S:
        del _armed[key]
        return None
    return entry


def _refusal(remaining_s: float) -> web.Response:
    # The wording avoids every phrase pi's retry classifier treats as transient
    # (status numbers, timeouts, "retry your request", ...): a retry here would
    # only be refused again. tests/test_summary_breaker.py pins this.
    minutes = max(1, math.ceil(remaining_s / 60))
    message = (
        "anthropic-throttle-proxy summary breaker: this session's previous context "
        "summary was cut by the client before it could finish, and a new attempt on a "
        f"larger context would be cut too. Summary requests for this session are refused "
        f"here for about {minutes} more min; nothing was sent upstream. Operators: "
        "THROTTLE_SUMMARY_BREAKER=off disables this."
    )
    return web.json_response(
        {"type": "error", "error": {"type": "invalid_request_error", "message": message}},
        status=400,
        headers={config.SUMMARY_BREAKER_HEADER: "1"},
    )
