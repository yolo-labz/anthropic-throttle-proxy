"""Pure parsers for upstream rate-limit headers, SSE usage, and request identity.

Everything here is side-effect-light: header → dict, body → model string, SSE
buffer → token counts, and the anonymized bearer/client identifiers used to key
the per-bearer limiters. The reactive ``_apply_unified`` (which reads the
monkeypatchable ``UTILIZATION_TARGET``) lives in :mod:`proxy`, not here, so the
test-suite can patch it on the ``proxy`` namespace.
"""

from __future__ import annotations

import hashlib
import json as _json
import math
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING

from .metrics import M_RATELIMIT_REQUESTS_REMAINING, M_RATELIMIT_TOKENS_REMAINING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aiohttp import web


def _bearer_id(headers: Mapping[str, str]) -> str:
    """Anonymized 8-hex bearer identifier. Keys per-bearer semaphores.

    Claude Code sends Authorization: Bearer <oauth-token>. Hashing the FULL
    header (including 'Bearer ' prefix) is fine — different bearers → different
    digests. Anonymous tag, never logs or exposes the token itself.

    Returns '_anon' when no auth header (health checks, /metrics, etc) so
    those requests share a single bypass slot rather than minting one slot
    per random caller.
    """
    auth = headers.get("Authorization") or headers.get("authorization")
    if not auth:
        return "_anon"
    return hashlib.sha256(auth.encode("utf-8", "replace")).hexdigest()[:8]


def _client_id(request: web.Request) -> str:
    """Identify the originating client connection for fair-queueing.

    Priority:
    1. X-Throttle-Client-Id header (explicit override).
    2. (peer_host, peer_port) tuple — unique per TCP connection on localhost.
    3. "_unknown" fallback.

    A claude-TUI keeps a keep-alive TCP, so its peer port stays stable for the
    session — exactly the discriminator the fair limiter needs. One PID with
    multiple TCPs still gets fair-shared across those TCPs, strictly better
    than the previous all-or-nothing behaviour.
    """
    cid = request.headers.get("X-Throttle-Client-Id")
    if cid:
        return cid
    peer = None
    try:
        peer = request.transport.get_extra_info("peername") if request.transport else None
    except Exception:
        peer = None
    if peer:
        return f"{peer[0]}:{peer[1]}"
    return "_unknown"


# Upstream rate-limit headers we surface for proactive pacing + diagnosis.
# Two families, depending on the auth regime:
#   API-key (pay-as-you-go): anthropic-ratelimit-{requests,tokens,...}-* with
#     remaining counts + RFC-3339 *-reset; retry-after (int seconds) on 429.
#   OAuth (Claude Code Max/Pro): anthropic-ratelimit-unified-* exposing 5h/7d
#     window UTILIZATION (0..1) + status (allowed/rejected) + epoch reset.
#     Measured 21/05/2026 against a Max-20x token — the OAuth family does NOT
#     include the remaining-count headers above, so utilization is the signal.
_RATELIMIT_HEADER_KEYS = (
    "retry-after",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-remaining",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-remaining",
    "anthropic-ratelimit-output-tokens-reset",
    # OAuth unified-window family.
    "anthropic-ratelimit-unified-status",
    "anthropic-ratelimit-unified-reset",
    "anthropic-ratelimit-unified-representative-claim",
    "anthropic-ratelimit-unified-5h-status",
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-status",
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-7d-reset",
)

_ZAI_QUOTA_CODES = {"1308", "1316", "1317"}


def _extract_ratelimit(headers: Mapping[str, str]) -> dict[str, str]:
    """Pull the subset of rate-limit headers we care about into a plain dict.

    Case-insensitive (aiohttp's CIMultiDict handles that). Returns only the
    keys that were actually present, so an empty dict means "upstream sent no
    rate-limit headers" — the key signal for the OAuth-vs-API-key question.
    """
    out = {}
    for key in _RATELIMIT_HEADER_KEYS:
        val = headers.get(key)
        if val is not None:
            out[key] = val
    return out


def _parse_retry_after(meta: Mapping[str, str] | None) -> float:
    """Seconds from a Retry-After header (integer form). 0.0 if absent/unparseable.

    Anthropic uses integer-seconds Retry-After; the HTTP-date form is not
    emitted by the Messages API, so we don't parse it.
    """
    raw = meta.get("retry-after") if meta else None
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _first_json_key(value: object, keys: set[str]) -> object | None:
    """Return the first value matching any key in a nested JSON object."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                return child
            found = _first_json_key(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_json_key(child, keys)
            if found is not None:
                return found
    return None


def _parse_zai_reset_epoch(raw: object) -> float | None:
    """Parse z.ai reset_time/resetAt values into epoch seconds.

    Numeric values are seconds or milliseconds since epoch. Naive datetime
    strings are treated as Beijing wall clock because z.ai's Coding Plan error
    bodies omit a timezone while the associated log IDs use UTC+08.
    """
    if raw is None:
        return None
    if isinstance(raw, int | float):
        value = float(raw)
        return value / 1000.0 if value > 1_000_000_000_000 else value
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is not None:
        return value / 1000.0 if value > 1_000_000_000_000 else value
    normalized = text.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if not re.search(r"[+-]\d{2}:?\d{2}$", normalized):
        normalized += "+08:00"
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _extract_zai_ratelimit_from_body(
    body: bytes | None,
    *,
    now: float | None = None,
    quota_jitter_s: float = 0.0,
) -> dict[str, str]:
    """Extract z.ai Coding Plan 429 details from a JSON response body.

    z.ai emits no Retry-After header for plan quota exhaustion. The reset is in
    the JSON body (`reset_time`, variants, or the older message text). We map
    that to the same metadata key the rest of the proxy already honors.
    """
    if not body:
        return {}
    try:
        payload = _json.loads(body)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    code_raw = _first_json_key(payload, {"code"})
    code = str(code_raw) if code_raw is not None else ""
    out: dict[str, str] = {}
    if code:
        out["zai-error-code"] = code

    reset_raw = _first_json_key(
        payload,
        {"reset_time", "resetTime", "reset_at", "resetAt", "resume_at", "resumeAt"},
    )
    reset_epoch = _parse_zai_reset_epoch(reset_raw)
    if reset_epoch is None:
        message = str(_first_json_key(payload, {"message"}) or "")
        match = re.search(r"reset at (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", message, re.I)
        if match:
            reset_epoch = _parse_zai_reset_epoch(match.group(1))

    if code in _ZAI_QUOTA_CODES:
        out["zai-quota-gate"] = "true"
    if reset_epoch is None:
        return out

    base_now = time.time() if now is None else now
    resume_epoch = max(reset_epoch, base_now) + max(0.0, quota_jitter_s)
    out["zai-reset-epoch"] = str(int(reset_epoch))
    out["zai-resume-epoch"] = str(int(resume_epoch))
    out["retry-after"] = str(max(0, int(math.ceil(resume_epoch - base_now))))
    return out


def _is_zai_quota_gate(meta: Mapping[str, str] | None) -> bool:
    """True when a z.ai response represents plan quota exhaustion."""
    if not meta:
        return False
    return meta.get("zai-quota-gate") == "true" or meta.get("zai-error-code") in _ZAI_QUOTA_CODES


def _publish_ratelimit_gauges(bid: str, meta: Mapping[str, str]) -> None:
    """Push numeric remaining-headroom headers into Prometheus gauges."""
    for key, gauge in (
        ("anthropic-ratelimit-requests-remaining", M_RATELIMIT_REQUESTS_REMAINING),
        ("anthropic-ratelimit-tokens-remaining", M_RATELIMIT_TOKENS_REMAINING),
    ):
        raw = meta.get(key)
        if raw is None:
            continue
        try:
            gauge.labels(bearer=bid).set(float(raw))
        except (TypeError, ValueError):
            pass


def _parse_unified(meta: Mapping[str, str] | None) -> dict[str, object]:
    """Parse the OAuth unified-window headers into a compact dict.

    Returns {} when none are present (e.g. API-key traffic), which is itself a
    useful signal. utilization is a 0..1 float; reset values are epoch seconds.
    """
    if not meta or not any(k.startswith("anthropic-ratelimit-unified") for k in meta):
        return {}

    def _f(key: str) -> float | None:
        """Parse header ``key`` as a float, or None if absent/unparseable."""
        try:
            v = meta.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _i(key: str) -> int | None:
        """Parse header ``key`` as an int (via float), or None if unparseable."""
        try:
            v = meta.get(key)
            return int(float(v)) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "status": meta.get("anthropic-ratelimit-unified-status"),
        "reset": _i("anthropic-ratelimit-unified-reset"),
        "representative_claim": meta.get("anthropic-ratelimit-unified-representative-claim"),
        "util_5h": _f("anthropic-ratelimit-unified-5h-utilization"),
        "status_5h": meta.get("anthropic-ratelimit-unified-5h-status"),
        "reset_5h": _i("anthropic-ratelimit-unified-5h-reset"),
        "util_7d": _f("anthropic-ratelimit-unified-7d-utilization"),
        "status_7d": meta.get("anthropic-ratelimit-unified-7d-status"),
        "reset_7d": _i("anthropic-ratelimit-unified-7d-reset"),
    }


def _binding_utilization(unified: Mapping[str, object]) -> float | None:
    """Utilization of the window Anthropic flags as representative.

    Falls back to ``max()`` of the available windows when the representative
    claim is missing or unknown.
    """
    u5h = unified.get("util_5h")
    u7d = unified.get("util_7d")
    claim = unified.get("representative_claim")
    if claim == "five_hour" and u5h is not None:
        return u5h
    if claim == "seven_day" and u7d is not None:
        return u7d
    candidates = [u for u in (u5h, u7d) if u is not None]
    return max(candidates) if candidates else None


def _binding_window(unified: Mapping[str, object]) -> str | None:
    """Name ("5h"/"7d") of the window ``_binding_utilization`` selects.

    Mirrors that helper's selection so the early-warning signal can label which
    window is binding. ``None`` when no utilization is present.
    """
    u5h = unified.get("util_5h")
    u7d = unified.get("util_7d")
    claim = unified.get("representative_claim")
    if claim == "five_hour" and u5h is not None:
        return "5h"
    if claim == "seven_day" and u7d is not None:
        return "7d"
    if u5h is None and u7d is None:
        return None
    if u7d is None:
        return "5h"
    if u5h is None:
        return "7d"
    # Tie-break matches max(): 7d only wins when strictly greater.
    return "7d" if u7d > u5h else "5h"


def _extract_model_from_body(body: bytes | None) -> str:
    """Pull the ``model`` field from a POST /v1/messages JSON body."""
    if not body:
        return ""
    try:
        obj = _json.loads(body)
        return obj.get("model", "") or ""
    except Exception:
        # Last-resort regex if the body isn't quite JSON.
        m = re.search(rb'"model"\s*:\s*"([^"]+)"', body)
        return m.group(1).decode("utf-8", "ignore") if m else ""


def _short_request_hint(body: bytes | None) -> tuple[int | None, bool]:
    """Return ``(max_tokens, has_tools)`` from a POST /v1/messages JSON body.

    Classifies short/latency-sensitive calls (the /goal Stop-hook evaluator:
    small ``max_tokens``, no ``tools``) for the limiter priority lane.
    ``max_tokens`` is None when absent or unparseable so the caller treats it
    as 'not obviously short' — fail-safe: an unknown shape stays in the normal
    lane, never priority, so a giant generation cannot jump the queue by
    accident.
    """
    if not body:
        return None, False
    try:
        obj = _json.loads(body)
    except (ValueError, TypeError):
        return None, False
    if not isinstance(obj, dict):
        return None, False
    mt = obj.get("max_tokens")
    max_tokens = mt if isinstance(mt, int) and not isinstance(mt, bool) else None
    tools = obj.get("tools")
    has_tools = isinstance(tools, list) and len(tools) > 0
    return max_tokens, has_tools


# Match a 'data: {...}' SSE line carrying a `usage` block. Streamed responses
# emit message_start (with input usage) and message_delta (with output usage).
_USAGE_RE = re.compile(rb'"usage"\s*:\s*\{[^}]+\}')


def _safe_load_usage(raw: bytes) -> dict | None:
    """JSON-decode one matched ``usage`` fragment, or None if it's malformed.

    Returns None instead of raising so the hot-path caller can skip a garbled
    SSE frame without a bare ``except: continue``.
    """
    try:
        obj = _json.loads(raw.split(b":", 1)[1].lstrip())
    except (ValueError, IndexError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse_sse_usage(buf: bytes) -> dict[str, int]:
    """Extract aggregated usage counts from a buffered SSE response.

    Returns a dict with input/output/cache_read/cache_creation token counts,
    summed across message_start + message_delta usage blocks (Anthropic emits
    both). Malformed usage fragments are skipped.
    """
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for match in _USAGE_RE.finditer(buf):
        usage_obj = _safe_load_usage(match.group(0))
        if usage_obj is None:
            continue
        # Anthropic field names → our shorter labels.
        totals["input"] += int(usage_obj.get("input_tokens") or 0)
        totals["output"] += int(usage_obj.get("output_tokens") or 0)
        totals["cache_read"] += int(usage_obj.get("cache_read_input_tokens") or 0)
        totals["cache_creation"] += int(usage_obj.get("cache_creation_input_tokens") or 0)
    return totals
