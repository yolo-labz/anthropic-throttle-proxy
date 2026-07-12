"""Anthropic API throttle proxy with optional central-tier fanout.

Two roles (same binary, different env):

LOCAL ROLE (default, ANTHROPIC_BASE_URL=http://127.0.0.1:8765):
    - Per-device proxy that every claude/opencode/codex client points at.
    - Tries to forward to $THROTTLE_CENTRAL_URL first; if that fails any
      health check (`/__throttle/health`) the request goes direct to
      $THROTTLE_UPSTREAM. So `dokku ps:scale anthropic-throttle 0` →
      transparent fallback to per-device serialization.
    - THROTTLE_QUEUE_MODE controls dispatch + observation independently:
      `off`     — pass-through. No queue, no AIMD counters.
      `observe` — no queue (PR #580). AIMD counters DO move on upstream
                  429/503/529, so /__throttle/health + Prometheus surface
                  early-warning signal while clients see no slow-down.
      `fair` / `reactive` — queue + AIMD. `reactive` is a legacy alias;
                  semantically identical to `fair`. Use `observe` if you
                  want AIMD signal WITHOUT queue stall.

CENTRAL ROLE (Dokku app on tailnet, no THROTTLE_CENTRAL_URL):
    - Uses the same queue mode as local. queue_mode=off means central is
      only a routing and observability point, not a bottleneck.
    - Listens on $THROTTLE_HOST:$THROTTLE_PORT — typical = 0.0.0.0:8765
      inside the container, exposed only to the tailnet by Dokku.

Health endpoint /__throttle/health returns: inflight, queued, served,
max_concurrent, upstream, central_url (or "" if local-direct), and the
most recent central-check status (`up`/`down`/`unknown`).

Module layout (PR: SonarQube clean-up). The hot-path helpers are split across
focused sibling modules — :mod:`config`, :mod:`metrics`, :mod:`pricing`,
:mod:`pacing`, :mod:`limiter`, :mod:`ratelimit`, :mod:`forwarding`. This module
keeps the request ``handler`` + control endpoints, the GROQ advisor wiring,
and re-exports the public names so ``from .. import proxy`` (ui.routes) and the
test-suite keep working unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import socket
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from . import config
from . import limiter as _limiter
from . import pacing as _pacing
from .body_shrink import shrink_body

# Re-exported config (env scalars + shared mutable state + log + HOP_HEADERS).
# These are the proxy module's stable public surface; ``ui.routes`` does
# ``from .. import proxy`` and reads ``proxy.MAX_CONCURRENT`` etc., and the
# test-suite imports the AIMD/advisor names from here. ``__all__`` (below)
# declares them as exports so they are not flagged as unused.
from .config import (
    AIMD_BACKOFF_S,
    AIMD_DECREASE,
    AIMD_INITIAL_CONCURRENT,
    AIMD_MIN,
    AIMD_RAMP_AFTER,
    AIMD_STATUSES,
    CENTRAL_FORWARD_TIMEOUT,
    CENTRAL_HEALTH_INTERVAL,
    CENTRAL_HEALTH_PATH,
    CENTRAL_HEALTH_TIMEOUT,
    CENTRAL_URL,
    HOP_HEADERS,
    LISTEN_HOST,
    LISTEN_PORT,
    MAX_CONCURRENT,
    MAX_HOLD_RETRY_AFTER_S,
    MIN_DISPATCH_GAP_S,
    OVERLOAD_STATUSES,
    QUEUE_MODE,
    RATE_PUSHBACK_RETRIES,
    THROTTLE_STATUSES,
    UPSTREAM,
    bearer_limiters,
    bearer_state,
    log,
    log_mode,
    state,
)
from .forwarding import (
    RetryableStatusError,
    _forward_once,
    central_health_loop,
    direct_target,
    pick_target,
    stamp_proxy_marker,
)
from .limiter import FairBearerLimiter, QueueWaitTimeout, _get_bearer_limiter
from .metrics import (
    CONTENT_TYPE_LATEST,
    M_ACCOUNT_COLLISIONS,
    M_ACCOUNT_SUSPECTED,
    M_AIMD_GROWS,
    M_AIMD_MAX,
    M_AIMD_OVERLOAD,
    M_AIMD_SHRINKS,
    M_BODY_SHRINK_BYTES_SAVED,
    M_BODY_SHRINK_TRIMMED,
    M_BRAKE_DISABLED_HOT,
    M_BRAKE_ENABLED,
    M_CENTRAL_STATUS,
    M_CLIENT_DISCONNECTS,
    M_COST,
    M_CREDENTIAL_NUDGE,
    M_DURATION,
    M_INFLIGHT,
    M_INFLIGHT_BEARER,
    M_KEEPALIVE_HOLDS,
    M_QUEUE_WAIT_TIMEOUTS,
    M_QUEUED,
    M_QUEUED_BEARER,
    M_REQUESTS,
    M_START_TIME,
    M_TOKENS,
    M_UPSTREAM_RETRIES,
    M_UTIL_5H,
    M_UTIL_7D,
    M_UTIL_WARNINGS,
    REGISTRY,
    generate_latest,
)
from .pricing import _pricing_for
from .ratelimit import (
    _api_key_id,
    _bearer_id,
    _binding_utilization,
    _binding_window,
    _client_id,
    _extract_model_from_body,
    _extract_ratelimit,
    _extract_zai_ratelimit_from_body,
    _is_zai_quota_gate,
    _parse_retry_after,
    _parse_sse_usage,
    _parse_unified,
    _publish_ratelimit_gauges,
    _short_request_hint,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

# Public surface re-exported from the focused sibling modules + defined here.
# Declared so static analysis treats the re-exports above as intentional.
__all__ = [
    # config scalars + shared state
    "AIMD_BACKOFF_S",
    "AIMD_DECREASE",
    "AIMD_INITIAL_CONCURRENT",
    "AIMD_MIN",
    "AIMD_RAMP_AFTER",
    "AIMD_STATUSES",
    "CENTRAL_FORWARD_TIMEOUT",
    "CENTRAL_HEALTH_INTERVAL",
    "CENTRAL_HEALTH_PATH",
    "CENTRAL_HEALTH_TIMEOUT",
    "CENTRAL_URL",
    "HOP_HEADERS",
    "LISTEN_HOST",
    "LISTEN_PORT",
    "MAX_CONCURRENT",
    "MAX_HOLD_RETRY_AFTER_S",
    "MIN_DISPATCH_GAP_S",
    "OVERLOAD_STATUSES",
    "QUEUE_MODE",
    "RATE_PUSHBACK_RETRIES",
    "THROTTLE_STATUSES",
    "UPSTREAM",
    "bearer_limiters",
    "bearer_state",
    "log",
    "log_mode",
    "state",
    # forwarding / limiter / pricing / ratelimit helpers
    "FairBearerLimiter",
    "_get_bearer_limiter",
    "_forward_once",
    "central_health_loop",
    "pick_target",
    "_pricing_for",
    "_bearer_id",
    "_binding_utilization",
    "_binding_window",
    "_client_id",
    "_extract_model_from_body",
    "_extract_ratelimit",
    "_extract_zai_ratelimit_from_body",
    "_is_zai_quota_gate",
    "_parse_retry_after",
    "_parse_sse_usage",
    "_parse_unified",
    "_publish_ratelimit_gauges",
    # metrics
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "generate_latest",
    "M_AIMD_GROWS",
    "M_AIMD_MAX",
    "M_AIMD_OVERLOAD",
    "M_AIMD_SHRINKS",
    "M_CREDENTIAL_NUDGE",
    "M_CENTRAL_STATUS",
    "M_CLIENT_DISCONNECTS",
    "M_COST",
    "M_DURATION",
    "M_INFLIGHT",
    "M_INFLIGHT_BEARER",
    "M_QUEUED",
    "M_QUEUED_BEARER",
    "M_REQUESTS",
    "M_START_TIME",
    "M_TOKENS",
    "M_UPSTREAM_RETRIES",
    "M_UTIL_5H",
    "M_UTIL_7D",
    "M_UTIL_WARNINGS",
    "M_KEEPALIVE_HOLDS",
    # defined in this module
    "UTILIZATION_TARGET",
    "UTILIZATION_WARN",
    "ADVISOR_ENABLED",
    "ADVISOR_DEBOUNCE_S",
    "_apply_unified",
    "_advisor_snapshot",
    "_maybe_advise",
    "handler",
    "health",
    "metrics",
    "root_probe",
    "main",
]

# WS-B2: OAuth unified-window utilization pacing. When > 0, once the binding
# window's utilization crosses this fraction (while still "allowed"), shrink the
# ceiling one AIMD step to ease off BEFORE hitting "rejected" — "glide near the
# limit without hitting it". 0 = disabled (surface utilization only). The
# proactive pause on an already-"rejected" window is unconditional (it just
# preempts a 429 you'd otherwise get).
#
# Defined HERE (not in config) because the test-suite monkeypatches it on the
# ``proxy`` namespace, and ``_apply_unified`` below reads the module global.
UTILIZATION_TARGET = float(os.environ.get("THROTTLE_UTILIZATION_TARGET", "0"))

# WS-B2 early-warning (observability only). When the binding unified window
# crosses this fraction while still "allowed", emit ONE WARNING line + a counter
# per (bearer, reset-window) — the pre-"rejected" signal the journal previously
# lacked (the 06/06/2026 5h-window incident logged nothing until the 429 itself).
# Warn-ONLY: it never shrinks the ceiling, so it is INDEPENDENT of
# UTILIZATION_TARGET (which IS a brake, default off) and safe to leave on. Set
# <= 0 to disable. Defined here (not config) for the same monkeypatch reason as
# UTILIZATION_TARGET.
UTILIZATION_WARN = float(os.environ.get("THROTTLE_UTILIZATION_WARN", "0.9"))

# How long a bearer's cached unified state stays trustworthy for classifying a
# 429 that arrived WITHOUT unified headers (see _budget_under_pressure). This is
# the discriminator between the two headerless-429 shapes: a concurrency storm
# interleaves successful streams that DO carry headers, so the cache keeps
# refreshing and stays fresh; a genuine budget wall 429s *every* response, so no
# refresh arrives and the cache ages past this window → we fall back to the
# conservative "assume budget" default. Long enough to bridge a concurrency
# storm's 429 bursts, short enough that a real wall is not read off a stale
# "allowed" sample. ponytail: fixed window, not a knob — tune here if the burst
# cadence changes.
UNIFIED_CACHE_FRESH_S = float(os.environ.get("THROTTLE_UNIFIED_CACHE_FRESH_S", "120"))

# Budget-paced routing (spec 4). Window lengths feed the elapsed-fraction pacing
# math; the elapsed-fraction FLOOR stops an early-cycle window (reset far away)
# from dividing pace toward infinity; the pacing SPAN is kept strictly below the
# inflight weight (10.0 in the score) so queue + inflight pressure always
# outweigh the pacing tie-breaker — the anti-dogpile invariant. The per-request
# util cost gives pacing an "after this request" projection; it is deliberately
# small because utilization is normalized 0..1 with no absolute cap on the hot
# path (a tier weight scales it, Opus/Fable heaviest).
_WINDOW_5H_S = 5 * 3600.0
_WINDOW_7D_S = 7 * 86400.0
_PACE_ELAPSED_FLOOR = 0.02
_PACING_SPAN = 9.0
_DEFAULT_REQUEST_UTIL_COST = 0.005
_TYPICAL_MAX_TOKENS = 4096.0
# Keep below one queued-request weight (100.0): a warning account stays protected
# under light inflight-only load, but strict-account queueing can spend it.
_WARNING_BACKPRESSURE_SURCHARGE = 80.0

# GROQ auto-advisor: on a throttle event, fire an out-of-band, debounced
# diagnosis to GROQ (an Anthropic-INDEPENDENT provider). Off by default; needs
# ADVISOR_ENABLED=true + GROQ_API_KEY. Never on the hot path: scheduled as a
# fire-and-forget task whose failures are swallowed. Defined here for the same
# monkeypatch reason as UTILIZATION_TARGET.
ADVISOR_ENABLED = os.environ.get("ADVISOR_ENABLED", "false").strip().lower() == "true"
ADVISOR_DEBOUNCE_S = float(os.environ.get("ADVISOR_DEBOUNCE_S", "120"))

# Strong refs to fire-and-forget tasks. asyncio only keeps a weak reference to
# a task, so a bare `create_task(...)` whose result is never awaited can be
# garbage-collected mid-flight. Hold the ref until the task is done.
_background_tasks: set[asyncio.Task] = set()

# Storm early-warning latch. The process-global upstream-retry counter only
# grows; we want ONE WARNING line each time it crosses STORM_WARN_RETRIES, not
# one per request once it's over. Latch on the way up, reset when the counter
# falls back below the threshold (a fresh process or a manual state reset) so a
# later storm warns again.
_storm_warned = False


def _maybe_warn_storm(retries: int) -> None:
    """Emit one WARNING line per upward crossing of ``config.STORM_WARN_RETRIES``.

    Reads ``config.STORM_WARN_RETRIES`` via attribute access so a future runtime
    override is honoured. No-op when the threshold is non-positive (disabled).
    """
    global _storm_warned
    threshold = config.STORM_WARN_RETRIES
    if threshold <= 0:
        return
    if retries >= threshold:
        if not _storm_warned:
            _storm_warned = True
            log(
                f"STORM WARNING: upstream retries={retries} exceeded "
                f"THROTTLE_STORM_WARN_RETRIES={threshold} — likely a "
                f"stale-token / 429 storm"
            )
    elif _storm_warned:
        _storm_warned = False


_last_advice_ts: float = 0.0
_direct_fallback_lock: asyncio.Lock | None = None
_api_key_cache: tuple[int, int, str, str] | None = None


def _api_key_bid(token: str) -> str:
    return _api_key_id(token)


def _is_api_key_bid(bid: str) -> bool:
    return bid.startswith("api-")


def _effective_admission(bid: str = "") -> tuple[str, int]:
    """Admission mode and hard cap for this request.

    Local deployments normally run ``THROTTLE_QUEUE_MODE=off`` because the
    central tier owns the fleet-wide fair queue. Runtime evidence from Opus
    4.7/1M bursts showed central-only admission reacts too late for same-host
    dogpiles, so a central-backed local proxy still keeps a small fair queue.
    """
    if _is_api_key_bid(bid):
        hard = config.API_KEY_MAX_CONCURRENT
        if config.QUEUE_MODE == "off" and config.CENTRAL_URL:
            return "fair", hard
        return config.QUEUE_MODE, hard
    if config.QUEUE_MODE == "off" and config.CENTRAL_URL:
        return "fair", max(1, min(config.CENTRAL_LOCAL_MAX_CONCURRENT, config.MAX_CONCURRENT))
    return config.QUEUE_MODE, config.MAX_CONCURRENT


def _get_direct_fallback_lock() -> asyncio.Lock:
    """Process-wide gate for direct retries after a central forward failure."""
    global _direct_fallback_lock
    if _direct_fallback_lock is None:
        _direct_fallback_lock = asyncio.Lock()
    return _direct_fallback_lock


def _per_reset_debounce_key(prefix: str, reset: int | None) -> str:
    """One stable key per (prefix, reset window) for once-per-window debouncing.

    Falls back to an AIMD-backoff time bucket when the reset epoch is missing,
    so a window with no reset header still debounces instead of firing per
    response. Shared by the warn signal and the opt-in glide.
    """
    if reset:
        return f"{prefix}:{reset}"
    cooldown = max(1.0, config.AIMD_BACKOFF_S)
    return f"{prefix}:bucket:{int(time.time() // cooldown)}"


def _maybe_warn_unified(
    bid: str,
    bstate: dict[str, object],
    unified: Mapping[str, object],
) -> None:
    """Emit ONE early-warning per (bearer, window, reset) when nearing the cap.

    Warn-ONLY: never shrinks the ceiling (that is ``UTILIZATION_TARGET``'s job,
    default off), so it is safe to leave on and gives the pre-"rejected" signal
    the journal previously lacked. Flat guard clauses keep cognitive complexity
    low. No-op when disabled or below threshold.
    """
    if UTILIZATION_WARN <= 0:
        return
    binding = _binding_utilization(unified)
    if binding is None or binding < UTILIZATION_WARN:
        return
    window = _binding_window(unified) or "?"
    reset = unified.get(f"reset_{window}") or unified.get("reset")
    warn_key = _per_reset_debounce_key(window, reset)
    if bstate.get("_util_warn_key") == warn_key:
        return
    bstate["_util_warn_key"] = warn_key
    M_UTIL_WARNINGS.labels(bearer=bid, window=window).inc()
    reset_in = int(reset - time.time()) if reset else -1
    log(
        f"unified-warning bid={bid} window={window} "
        f"util={binding:.2f}>={UTILIZATION_WARN:.2f} reset_in={reset_in}s "
        "(approaching cap, still allowed)"
    )
    _note_brake_disabled_hot(bid, bstate, unified)


def _note_brake_disabled_hot(
    bid: str,
    bstate: dict[str, object],
    unified: Mapping[str, object],
) -> None:
    """Fire the disabled-brake visibility ONCE per (bearer, window, reset).

    An account is 'hot while unbraked' whether it is APPROACHING the cap (the
    warn path) or ALREADY rejected/hard-locked. Codex MAJOR: the rejected path
    returns before the warn, so a sample first seen as ``rejected`` never fired
    the metric that exists precisely to surface that hard-lock. Own debounce key
    so it fires once regardless of which path observes it first. No-op when the
    brake is armed.
    """
    if UTILIZATION_TARGET > 0:
        return
    window = _binding_window(unified) or "?"
    reset = unified.get(f"reset_{window}") or unified.get("reset")
    key = _per_reset_debounce_key(window, reset)
    if bstate.get("_brake_hot_key") == key:
        return
    bstate["_brake_hot_key"] = key
    M_BRAKE_DISABLED_HOT.labels(bearer=bid, window=window).inc()
    log(
        f"BRAKE DISABLED bid={bid} window={window} (THROTTLE_UTILIZATION_TARGET=0) — "
        "no glide; this window hard-locks at 1.0 into a multi-day lockout. "
        "Set the target to brake."
    )


def _publish_unified_gauges(bid: str, unified: Mapping[str, object]) -> None:
    """Mirror the 5h/7d utilization fractions onto their Prometheus gauges."""
    if unified.get("util_5h") is not None:
        M_UTIL_5H.labels(bearer=bid).set(unified["util_5h"])
    if unified.get("util_7d") is not None:
        M_UTIL_7D.labels(bearer=bid).set(unified["util_7d"])


def _maybe_pause_rejected(
    bid: str,
    limiter: FairBearerLimiter,
    unified: Mapping[str, object],
) -> bool:
    """Pause the bearer until reset when a window is already "rejected".

    Returns ``True`` when a window is rejected (caller must stop) — this
    preempts the 429 + ClientConnectionReset storm of hammering an exhausted
    cap. ``False`` when no window is rejected.
    """
    rejected = "rejected" in (
        unified.get("status"),
        unified.get("status_5h"),
        unified.get("status_7d"),
    )
    if not rejected:
        return False
    reset = unified.get("reset") or unified.get("reset_5h") or unified.get("reset_7d") or 0
    pause = reset - time.time()
    if pause > 0:
        limiter.note_retry_after(pause)
        log(f"unified-rejected bid={bid} pause={int(pause)}s until reset (proactive 429-avoid)")
    return True


async def _maybe_glide(
    bid: str,
    bstate: dict[str, object],
    limiter: FairBearerLimiter,
    unified: Mapping[str, object],
) -> None:
    """Opt-in proactive shrink as the binding window nears the cap (WS-B2).

    No-op unless ``UTILIZATION_TARGET > 0`` and the binding window crosses it.
    Shrinks ONCE per reset window — repeated per-response shrink collapses an
    active swarm to one slot without any real 429/503 signal. Default off.
    """
    if UTILIZATION_TARGET <= 0:
        return
    binding = _binding_utilization(unified)
    if binding is None or binding < UTILIZATION_TARGET:
        return
    reset = unified.get("reset") or unified.get("reset_5h") or unified.get("reset_7d")
    # Prefix is the bare target so the key is byte-identical to the pre-refactor
    # format — a hot code-reload that keeps bearer_state won't shrink twice.
    shrink_key = _per_reset_debounce_key(str(UTILIZATION_TARGET), reset)
    if bstate.get("_util_shrink_key") == shrink_key:
        return
    new_max = await limiter.shrink()
    if new_max is None:
        return
    bstate["_util_shrink_key"] = shrink_key
    M_AIMD_SHRINKS.labels(bearer=bid, status="util").inc()
    M_AIMD_MAX.labels(bearer=bid).set(new_max)
    log(f"util-shrink bid={bid} util={binding:.2f}>={UTILIZATION_TARGET} max_concurrent={new_max}")


async def _apply_unified(
    bid: str,
    bstate: dict[str, object],
    limiter: FairBearerLimiter,
    meta: Mapping[str, str],
) -> None:
    """React to OAuth unified-window headers (WS-B2).

    A flat pipeline of single-purpose helpers (each kept low-complexity):
    1. ``_publish_unified_gauges`` — surface utilization, always.
    2. ``_maybe_pause_rejected`` — pause until reset if a window is already
       "rejected"; preempts the 429 + ClientConnectionReset storm.
    2b. ``_maybe_warn_unified`` — log one early-warning per window before the
        cap (observability only, never shrinks).
    3. ``_maybe_glide`` — opt-in proactive shrink near the cap
       (``UTILIZATION_TARGET > 0``, default off).

    Never raises into the hot path (caller wraps in try/except).
    """
    unified = _parse_unified(meta)
    if not unified:
        return
    bstate["unified"] = unified
    # Stamp when this sample landed so a headerless-429 classification can tell a
    # fresh cache (concurrency storm, still receiving header-bearing successes)
    # from a stale one (budget wall, every response 429ing) — see
    # _budget_under_pressure / UNIFIED_CACHE_FRESH_S.
    bstate["unified_at"] = time.time()
    _publish_unified_gauges(bid, unified)
    if _maybe_pause_rejected(bid, limiter, unified):
        # Rejected = already hard-locked. If the brake is off, this IS the
        # "hard-lock while unbraked" event the visibility metric must catch —
        # the warn path below is never reached (Codex MAJOR).
        _note_brake_disabled_hot(bid, bstate, unified)
        return
    _maybe_warn_unified(bid, bstate, unified)
    await _maybe_glide(bid, bstate, limiter, unified)


def _advisor_snapshot(
    trigger_bid: str | None = None,
    trigger_status: int | None = None,
) -> dict[str, object]:
    """Assemble a JSON-safe view of proxy state for the advisor.

    Mirrors the dashboard's ``ui.routes._collect_view`` but adds the throttle
    trigger and avoids importing ui.routes, so it stays callable from the
    hot-path finally block without a circular import.
    """
    bearers = []
    for b, bs in bearer_state.items():
        lim = bearer_limiters.get(b)
        bearers.append(
            {
                "bearer_id": b,
                "inflight": bs.get("inflight", 0),
                "queued": bs.get("queued", 0),
                "served": bs.get("served", 0),
                "last_ratelimit": bs.get("last_ratelimit"),
                "unified": bs.get("unified"),
                "limiter": lim.snapshot() if lim is not None else None,
            }
        )
    return {
        "inflight": state["inflight"],
        "queued": state["queued"],
        "served": state["served"],
        "disconnects": state["client_disconnects"],
        "retries": state["upstream_retries"],
        "max_concurrent": config.MAX_CONCURRENT,
        "queue_mode": config.QUEUE_MODE,
        "min_dispatch_gap_ms": int(config.MIN_DISPATCH_GAP_S * 1000),
        "upstream": config.UPSTREAM,
        "central_url": config.CENTRAL_URL or "(direct)",
        "central_status": state["central_status"],
        "trigger": (
            {"bearer": trigger_bid, "status": trigger_status}
            if trigger_status is not None
            else None
        ),
        "bearers": bearers,
    }


async def _maybe_advise(trigger_bid: str, trigger_status: int) -> None:
    """Fire-and-forget GROQ diagnosis on a throttle event.

    Debounced to at most once per ``ADVISOR_DEBOUNCE_S`` so a 429 storm can't
    turn into a GROQ storm (GROQ's own free tier is ~30 RPM). Never raises —
    any failure is stored as the diagnosis text and logged. Runs in its own
    task, so the proxy hot path is unaffected regardless of outcome.
    """
    global _last_advice_ts
    if not ADVISOR_ENABLED or not os.environ.get("GROQ_API_KEY"):
        return
    now = time.time()
    if now - _last_advice_ts < ADVISOR_DEBOUNCE_S:
        return
    # Claim the debounce window before awaiting (cheap de-dupe).
    _last_advice_ts = now
    trigger = f"status={trigger_status} bid={trigger_bid}"
    try:
        from .ui.advisor_impl import recommend

        text = await recommend(_advisor_snapshot(trigger_bid, trigger_status))
        state["last_advisor"] = {"text": text, "ts": now, "trigger": trigger}
        log(f"advisor {trigger}: {text[:160]!r}")
    except Exception as exc:
        state["last_advisor"] = {
            "text": f"(advisor error: {exc!s})",
            "ts": now,
            "trigger": trigger,
        }
        log(f"advisor-error {trigger}: {exc!r}")


# --- request handler, split into helpers to keep cognitive complexity low ----

# Client-side disconnects we must NOT retry upstream (the client gave up).
_CLIENT_DISCONNECT_EXC = (
    ConnectionResetError,
    aiohttp.ClientConnectionResetError,
    asyncio.CancelledError,
)


class _Counters:
    """Bundle of the three nested counter scopes touched per request.

    ``s`` is the process-global ``state`` dict, ``b`` the per-bearer dict, and
    ``c`` the per-client dict. Grouping them lets the queue/inflight bookkeeping
    move into small helpers without threading three arguments everywhere.
    """

    def __init__(
        self, bid: str, cid: str, bstate: dict[str, object], cstate: dict[str, int]
    ) -> None:
        self.bid = bid
        self.cid = cid
        self.s = state
        self.b = bstate
        self.c = cstate
        self.queued_incremented = False

    def enqueue(self, request: web.Request, path: str) -> None:
        """Increment the queue counters (queue modes only) + publish gauges."""
        self.s["queued"] += 1
        self.b["queued"] += 1
        self.c["queued"] += 1
        self.queued_incremented = True
        M_QUEUED.set(self.s["queued"])
        M_QUEUED_BEARER.labels(bearer=self.bid).set(self.b["queued"])
        log(
            f"queue+ method={request.method} path=/{path} bid={self.bid} "
            f"cid={self.cid} inflight={self.s['inflight']} queued={self.s['queued']}"
        )

    def dequeue(self) -> None:
        """Roll the queue counters back (slot acquired, or cancellation rollback)."""
        if not self.queued_incremented:
            return
        self.s["queued"] -= 1
        self.b["queued"] -= 1
        self.c["queued"] -= 1
        self.queued_incremented = False
        M_QUEUED.set(self.s["queued"])
        M_QUEUED_BEARER.labels(bearer=self.bid).set(self.b["queued"])

    def enter_inflight(self) -> None:
        """Bump the in-flight counters once a slot is held + publish gauges."""
        self.s["inflight"] += 1
        self.b["inflight"] += 1
        self.c["inflight"] += 1
        M_QUEUED.set(self.s["queued"])
        M_QUEUED_BEARER.labels(bearer=self.bid).set(self.b["queued"])
        M_INFLIGHT.set(self.s["inflight"])
        M_INFLIGHT_BEARER.labels(bearer=self.bid).set(self.b["inflight"])

    def exit_inflight(self, final_status: int) -> None:
        """Drop the in-flight counters + record served on a 2xx/3xx."""
        self.s["inflight"] -= 1
        self.b["inflight"] -= 1
        self.c["inflight"] -= 1
        if final_status and 200 <= final_status < 400:
            self.b["served"] += 1
            self.c["served"] += 1
        M_INFLIGHT.set(self.s["inflight"])
        M_INFLIGHT_BEARER.labels(bearer=self.bid).set(self.b["inflight"])


class _Attempt:
    """Mutable result accumulator for a request's forward attempt(s)."""

    def __init__(self) -> None:
        self.response: web.StreamResponse | None = None
        self.final_status = 0
        self.captured: bytearray | None = None
        self.meta: dict[str, str] | None = None
        self.started_at: float | None = None
        self.context: dict[str, str] = {}
        # Set by the keepalive-hold, which applies AIMD feedback per-throttle
        # itself. _finalize must then SKIP its own _aimd_feedback so an
        # exhausted 529/queue-timeout hold (terminal synthetic 503) does not
        # AIMD-shrink the bearer (invariants 7 + 9; Codex panel BLOCKER).
        self.aimd_owned = False


def _record_disconnect(
    path: str, where: str, exc: BaseException, attempt: _Attempt
) -> web.Response:
    """Account a client disconnect (no upstream retry) and build a 499 reply."""
    state["client_disconnects"] += 1
    M_CLIENT_DISCONNECTS.inc()
    parts = ["client-disconnect", f"where={where}", f"path=/{path}"]
    for key in ("method", "bid", "cid", "via", "model"):
        value = attempt.context.get(key)
        if value:
            parts.append(f"{key}={value}")
    if attempt.started_at is not None:
        parts.append(f"elapsed_ms={int((time.time() - attempt.started_at) * 1000)}")
    exc_text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    parts.append(f"exc={exc_text!r}")
    parts.append("no_upstream_retry=true")
    log(" ".join(parts))
    attempt.final_status = 499
    return web.Response(status=499)


def _request_disconnected(request: web.Request) -> bool:
    """True when aiohttp no longer has an open client transport."""
    transport = request.transport
    return transport is None or transport.is_closing()


def _is_queue_timeout_response(response: web.StreamResponse | None) -> bool:
    """True for a proxy-generated queue-wait-timeout 503 (ours or relayed).

    The local tier must hand these to the client verbatim: pushback-retrying
    would spin against the same full central queue, and AIMD-shrinking would
    misattribute central's queue depth to this bearer's upstream behavior.
    """
    return response is not None and config.QUEUE_TIMEOUT_HEADER in response.headers


def _queue_wait_timeout_response(
    bid: str, cid: str, path: str, limiter: FairBearerLimiter, max_wait: float
) -> web.Response:
    """Fail a queued request fast with a clean 503 while the client transport
    is still alive.

    Answering INSIDE claude's patience window (it aborts a silent request at
    ~60 s) is the whole point: a response written after the client hangs up
    lands on a closing transport and surfaces client-side as truncated HTTP →
    ``InvalidHTTPResponse`` → a phantom 401/login error (07/07/2026 incident).
    A clean 503 + Retry-After makes the SDK retry transparently and re-enter
    the round-robin fairly.
    """
    M_QUEUE_WAIT_TIMEOUTS.labels(bearer=bid).inc()
    snap = limiter.snapshot()
    log(
        f"queue-wait-timeout bid={bid} cid={cid} path=/{path} "
        f"max_wait_s={max_wait:g} inflight={snap['inflight']} "
        f"queued_total={snap['queued_total']} max_concurrent={snap['max_concurrent']}"
    )
    return web.Response(
        status=503,
        headers={
            "retry-after": str(config.QUEUE_TIMEOUT_RETRY_AFTER_S),
            config.QUEUE_TIMEOUT_HEADER: "1",
        },
        text=(
            "proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue\n"
        ),
    )


def _effective_queue_max_wait(headers: Mapping[str, str]) -> float | None:
    """This tier's queue-wait bound: min(local knob, inherited budget).

    Without the inherited term the local and central bounds STACK — 30 s in
    the local queue plus 30 s in central's puts the client at ~60 s of
    silence, exactly the abort threshold the bound exists to stay under
    (Codex BLOCKER on PR #83). ``None`` = unbounded (knob off, no budget
    header). A client-supplied budget can only shorten its own wait — min()
    never exceeds the local knob — so the header needs no trust filtering.
    """
    knob = config.QUEUE_MAX_WAIT_S or None
    raw = headers.get(config.WAIT_BUDGET_HEADER)
    if raw is None:
        return knob
    try:
        inherited = max(0.0, float(raw) / 1000.0)
    except ValueError:
        return knob
    return inherited if knob is None else min(knob, inherited)


def _is_priority_request(max_tokens: int | None, has_tools: bool, body_len: int) -> bool:
    """Classify a request for the limiter priority lane.

    Priority = provably short: a parsed 0 < max_tokens ≤ PRIORITY_MAX_TOKENS,
    no tools, and a body ≤ PRIORITY_MAX_BODY_BYTES. Every gate fails safe
    toward the normal lane.
    """
    return (
        max_tokens is not None
        and 0 < max_tokens <= config.PRIORITY_MAX_TOKENS
        and not has_tools
        and body_len <= config.PRIORITY_MAX_BODY_BYTES
    )


def _account_routing_enabled() -> bool:
    return config.ACCOUNT_ROUTING_MODE in {"least_loaded", "budget_paced"} and bool(
        config.ACCOUNT_CRED_PATHS
    )


def _api_key_routing_enabled() -> bool:
    return config.API_KEY_ROUTING_MODE in {"overflow", "prefer"} and bool(config.API_KEY_FILE)


def _api_key_candidate() -> dict[str, object] | None:
    """Return the configured API-key candidate, or None when disabled/unusable.

    The key stays in a 0600 runtime file. The hot path caches by (mtime, size)
    so a request normally pays one stat(), while key rotation is picked up
    without a restart. The raw key is returned only to the header-rewrite helper.
    """
    if not _api_key_routing_enabled():
        return None
    try:
        st = os.stat(config.API_KEY_FILE)
    except OSError:
        return None
    global _api_key_cache
    if _api_key_cache is None or _api_key_cache[:2] != (st.st_mtime_ns, st.st_size):
        try:
            with open(config.API_KEY_FILE, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            return None
        if not token:
            return None
        _api_key_cache = (st.st_mtime_ns, st.st_size, _api_key_bid(token), token)
    bid, token = _api_key_cache[2], _api_key_cache[3]
    lim = config.bearer_limiters.get(bid)
    if lim is not None and getattr(lim, "retry_after_remaining", lambda: 0.0)() > 0:
        return None
    return {
        "label": config.API_KEY_LABEL,
        "bearer_id": bid,
        "token": token,
        "auth_type": "api_key",
    }


def _model_tier(model: str) -> str:
    """Coarse model tier from a model id / display name, or "" when unknown.

    ``claude-sonnet-4-6`` → ``sonnet``, ``Fable`` → ``fable``. Matches on exact
    separator-split tokens (not raw substrings, so ``sonnets`` ≠ ``sonnet``) and
    treats an ambiguous id carrying >1 tier token as unknown (Codex LOW). Used
    to align a request with the account's scoped (per-model) weekly meter.
    """
    normalized = model.lower()
    for sep in "-_./":
        normalized = normalized.replace(sep, " ")
    tokens = set(normalized.split())
    tiers = [tier for tier in ("opus", "sonnet", "haiku", "fable") if tier in tokens]
    return tiers[0] if len(tiers) == 1 else ""


def _append_window(
    windows: list[tuple[float, float | None, float]],
    util: object,
    reset: object,
    length: float,
) -> None:
    """Record a ``(utilization, reset-epoch|None, window-length-s)`` budget window.

    Only appends when ``util`` is numeric; a non-numeric/absent reset becomes
    ``None`` (priced on utilization alone, no deadline discount).
    """
    if isinstance(util, (int, float)):
        reset_epoch = float(reset) if isinstance(reset, (int, float)) else None
        windows.append((float(util), reset_epoch, length))


def _expected_request_util_cost(model: str, max_tokens: int | None = None) -> float:
    """Projected utilization one request adds — pacing's "after this request" view.

    Utilization is normalized 0..1 with no absolute cap exposed on the hot path,
    so this is a deliberately small heuristic: a tier weight (Opus/Fable heaviest)
    times a token factor when ``max_tokens`` is cheaply known, else a conservative
    constant. Ceiling: the token factor is capped at 4× so one huge request cannot
    dominate the deadline signal.
    """
    tier_weight = {"opus": 2.0, "fable": 2.0, "sonnet": 1.0, "haiku": 0.5}.get(
        _model_tier(model), 1.0
    )
    token_factor = 1.0
    if isinstance(max_tokens, int) and max_tokens > 0:
        token_factor = min(max_tokens / _TYPICAL_MAX_TOKENS, 4.0)
    return _DEFAULT_REQUEST_UTIL_COST * tier_weight * token_factor


def _budget_pacing_pressure(
    windows: list[tuple[float, float | None, float]], expected_cost: float, now: float
) -> float:
    """Deadline-aware pacing price for a candidate account (lower is cheaper).

    Water-filling / max-min fairness: the tightest (binding) window sets the
    price, so we take the MAX per-window pace — routing then minimizes it across
    accounts, keeping the most-constrained budget as loose as possible. Each
    window's pace is ``projected_util ÷ elapsed-cycle-fraction`` (the same dual
    price as the accounts-view burn meter): a window early in its cycle burning
    fast is expensive, one near its reset with slack is cheap. ``projected_util``
    folds in the expected request cost so a candidate is priced AFTER admitting
    the request. The result is a bounded monotone map into ``[0, _PACING_SPAN)``
    so queue/inflight/retry gates always outweigh pacing (anti-dogpile). A
    bearer in ``allowed_warning`` gets a finite surcharge when pressure is
    allowed, so warning accounts stay protected under light load but can beat a
    non-warning bearer whose local queue is already large enough to surface
    client retries.
    """
    worst = 0.0
    for util, reset, length in windows:
        projected = min(util + expected_cost, 1.0)
        if reset is None or reset <= now or length <= 0:
            pace = projected  # no usable deadline → price on projected util alone
        else:
            elapsed_frac = (length - (reset - now)) / length
            elapsed_frac = min(max(elapsed_frac, _PACE_ELAPSED_FLOOR), 1.0)
            pace = projected / elapsed_frac
        worst = max(worst, pace)
    return _PACING_SPAN * worst / (worst + 1.0)


def _account_routing_candidate_score(
    acct: dict[str, object],
    incoming_bid: str,
    *,
    allow_pressure: bool = False,
    model: str = "",
    max_tokens: int | None = None,
    now: float | None = None,
) -> float:
    """Lower is better for account routing. Uses only live pressure, no secrets.

    ``model`` (spec 3) makes routing model-aware: when the request's tier matches
    the account's scoped (per-model) weekly meter, that meter's utilization is
    folded into the pressure — a Sonnet request avoids an account whose Sonnet
    meter is near cap even if its all-models window still has room.

    Two ranking modes share ONE gate structure (``config.ACCOUNT_ROUTING_MODE``):
    ``least_loaded`` ranks on raw max utilization; ``budget_paced`` (spec 4) ranks
    on deadline-aware pacing over each observed 5h/7d/scoped budget window. The
    hard gates (retry-after, rejected, util≥1.0, endpoint 429, warning-pressure
    without ``allow_pressure``) and the queue/inflight prefix are identical in
    both modes — only the sub-gate tie-breaker term differs.
    """
    now = time.time() if now is None else now
    bid = acct.get("bearer_id")
    if not isinstance(bid, str) or not bid:
        return math.inf
    lim = config.bearer_limiters.get(bid)
    if lim is not None and getattr(lim, "retry_after_remaining", lambda: 0.0)() > 0:
        return math.inf
    snap = lim.snapshot() if lim is not None and hasattr(lim, "snapshot") else {}
    queued = float(snap.get("queued_total") or 0)
    priority_queued = float(snap.get("priority_queued") or 0)
    inflight = float(snap.get("inflight") or 0)
    # (util, reset-epoch|None, window-length) per observed budget window. Fed to
    # the budget_paced ranker below; least_loaded ignores it (uses max util only).
    windows: list[tuple[float, float | None, float]] = []
    under_pressure = False
    bstate = config.bearer_state.get(bid, {})
    unified = bstate.get("unified") if isinstance(bstate, dict) else None
    if isinstance(unified, dict):
        statuses = (unified.get("status"), unified.get("status_5h"), unified.get("status_7d"))
        if "rejected" in statuses:
            return math.inf
        under_pressure = any(status == "allowed_warning" for status in statuses)
        util = max(
            float(v)
            for v in (unified.get("util_5h"), unified.get("util_7d"), 0.0)
            if isinstance(v, (int, float))
        )
        _append_window(windows, unified.get("util_5h"), unified.get("reset_5h"), _WINDOW_5H_S)
        _append_window(windows, unified.get("util_7d"), unified.get("reset_7d"), _WINDOW_7D_S)
        under_pressure = under_pressure or (UTILIZATION_WARN > 0 and util >= UTILIZATION_WARN)
        if under_pressure and not allow_pressure:
            return math.inf
    else:
        util = 0.0
    endpoint = acct.get("endpoint")
    if isinstance(endpoint, dict):
        if "(429)" in str(endpoint.get("err") or ""):
            return math.inf
        usage = endpoint.get("usage")
        if isinstance(usage, dict):
            endpoint_util = max(
                float(v)
                for v in (usage.get("util_5h"), usage.get("util_7d"), 0.0)
                if isinstance(v, (int, float))
            )
            if endpoint_util >= 1.0:
                return math.inf
            util = max(util, endpoint_util)
            _append_window(windows, usage.get("util_5h"), usage.get("reset_5h"), _WINDOW_5H_S)
            _append_window(windows, usage.get("util_7d"), usage.get("reset_7d"), _WINDOW_7D_S)
            if UTILIZATION_WARN > 0 and endpoint_util >= UTILIZATION_WARN and not allow_pressure:
                return math.inf
            under_pressure = under_pressure or (
                UTILIZATION_WARN > 0 and endpoint_util >= UTILIZATION_WARN
            )
            # spec 3: model-aware — if this request's tier matches the account's
            # scoped weekly meter, fold that meter's utilization in (it is the
            # binding budget for THIS request even when all-models has room).
            scoped = usage.get("scoped")
            if model and isinstance(scoped, dict):
                s_util = scoped.get("util")
                if isinstance(s_util, (int, float)) and _model_tier(model) == _model_tier(
                    str(scoped.get("model") or "")
                ):
                    if s_util >= 1.0:
                        return math.inf
                    util = max(util, float(s_util))
                    # scoped is a per-model 7d budget; its reset (when present) prices it.
                    _append_window(windows, s_util, scoped.get("reset"), _WINDOW_7D_S)
                    if UTILIZATION_WARN > 0 and s_util >= UTILIZATION_WARN and not allow_pressure:
                        return math.inf
                    under_pressure = under_pressure or (
                        UTILIZATION_WARN > 0 and s_util >= UTILIZATION_WARN
                    )
    # Queue dominates; utilization/pacing is a soft tie-breaker below the warn line.
    stickiness = -0.01 if bid == incoming_bid else 0.0
    load = queued * 100.0 + priority_queued * 100.0 + inflight * 10.0
    warning_surcharge = _WARNING_BACKPRESSURE_SURCHARGE if under_pressure else 0.0
    if config.ACCOUNT_ROUTING_MODE == "budget_paced":
        pressure = _budget_pacing_pressure(
            windows, _expected_request_util_cost(model, max_tokens), now
        )
        return load + warning_surcharge + pressure + stickiness
    return load + warning_surcharge + util * 5.0 + stickiness


def _bearer_local_load_score(bid: str) -> float:
    """Queue/inflight pressure only; lower is better for same-host admission."""
    lim = config.bearer_limiters.get(bid)
    snap = lim.snapshot() if lim is not None and hasattr(lim, "snapshot") else {}
    queued = float(snap.get("queued_total") or 0)
    priority_queued = float(snap.get("priority_queued") or 0)
    inflight = float(snap.get("inflight") or 0)
    return queued * 100.0 + priority_queued * 100.0 + inflight * 10.0


def _route_to_selected_auth(
    headers: dict[str, str], incoming_bid: str, selected: dict[str, object]
) -> tuple[str, str | None]:
    selected_bid = str(selected["bearer_id"])
    for key in list(headers):
        if key.lower() in {"authorization", "x-api-key"}:
            del headers[key]
    token = str(selected["token"])
    label = str(selected.get("label") or "")
    if selected.get("auth_type") == "api_key":
        headers["x-api-key"] = token
        if selected_bid != incoming_bid:
            log(f"api-key-route from={incoming_bid} to={selected_bid} label={label or '?'}")
    else:
        headers["Authorization"] = f"Bearer {token}"
        if selected_bid != incoming_bid:
            log(f"account-route from={incoming_bid} to={selected_bid} label={label or '?'}")
    return selected_bid, label


def _healthy_known_unconfigured_bearer(
    incoming_bid: str, configured_bids: set[str], best_configured_load: float
) -> bool:
    """True when an incoming non-configured bearer has fresh no-pressure evidence."""
    if not incoming_bid or incoming_bid in configured_bids:
        return False
    limiter = config.bearer_limiters.get(incoming_bid)
    if limiter is None:
        return False
    if getattr(limiter, "retry_after_remaining", lambda: 0.0)() > 0:
        return False
    bstate = config.bearer_state.get(incoming_bid)
    if not isinstance(bstate, dict):
        return False
    unified = bstate.get("unified")
    unified_at = bstate.get("unified_at")
    if not (
        isinstance(unified, dict)
        and isinstance(unified_at, (int, float))
        and (time.time() - unified_at) <= UNIFIED_CACHE_FRESH_S
    ):
        return False
    statuses = (unified.get("status"), unified.get("status_5h"), unified.get("status_7d"))
    if any(status in ("allowed_warning", "rejected") for status in statuses):
        return False
    binding = _binding_utilization(unified)
    if binding is None or (UTILIZATION_WARN > 0 and binding >= UTILIZATION_WARN):
        return False
    return _bearer_local_load_score(incoming_bid) <= best_configured_load


def _route_account_if_enabled(
    headers: dict[str, str],
    incoming_bid: str,
    *,
    method: str,
    path: str,
    model: str = "",
    max_tokens: int | None = None,
) -> tuple[str, str | None]:
    """Optionally rewrite upstream Authorization to a configured account.

    Returns ``(bearer_id_used_for_limiter, account_label_or_none)``. Raw tokens
    are kept inside the header dict sent upstream and are never logged. ``model``
    (spec 3) biases selection toward the account with headroom on that model's
    scoped weekly meter.
    """
    if method != "POST" or "v1/messages" not in path:
        return incoming_bid, None
    api_key = _api_key_candidate()
    if api_key is not None and config.API_KEY_ROUTING_MODE == "prefer":
        return _route_to_selected_auth(headers, incoming_bid, api_key)
    if not _account_routing_enabled():
        if api_key is not None:
            return _route_to_selected_auth(headers, incoming_bid, api_key)
        return incoming_bid, None
    from . import accounts

    # One clock for the whole selection: the snapshot AND every pacing score must
    # measure time-to-reset against the same ``now`` (budget_paced divides by it).
    now = time.time()
    snapshot = accounts.routing_snapshot(now)
    configured_bids = {
        str(acct["bearer_id"])
        for acct in snapshot
        if isinstance(acct.get("token"), str) and isinstance(acct.get("bearer_id"), str)
    }
    strict_candidates = [
        acct
        for acct in snapshot
        if isinstance(acct.get("token"), str)
        and isinstance(acct.get("bearer_id"), str)
        and _account_routing_candidate_score(
            acct, incoming_bid, model=model, max_tokens=max_tokens, now=now
        )
        < math.inf
    ]
    best_configured_load = min(
        (_bearer_local_load_score(str(acct["bearer_id"])) for acct in strict_candidates),
        default=math.inf,
    )
    if _healthy_known_unconfigured_bearer(incoming_bid, configured_bids, best_configured_load):
        return incoming_bid, None

    pressure_candidates = [
        acct
        for acct in snapshot
        if isinstance(acct.get("token"), str)
        and isinstance(acct.get("bearer_id"), str)
        and _account_routing_candidate_score(
            acct,
            incoming_bid,
            allow_pressure=True,
            model=model,
            max_tokens=max_tokens,
            now=now,
        )
        < math.inf
    ]
    candidates = strict_candidates or pressure_candidates
    if strict_candidates and pressure_candidates:
        strict_best = min(
            strict_candidates,
            key=lambda acct: _account_routing_candidate_score(
                acct,
                incoming_bid,
                allow_pressure=True,
                model=model,
                max_tokens=max_tokens,
                now=now,
            ),
        )
        pressure_best = min(
            pressure_candidates,
            key=lambda acct: _account_routing_candidate_score(
                acct,
                incoming_bid,
                allow_pressure=True,
                model=model,
                max_tokens=max_tokens,
                now=now,
            ),
        )
        strict_score = _account_routing_candidate_score(
            strict_best,
            incoming_bid,
            allow_pressure=True,
            model=model,
            max_tokens=max_tokens,
            now=now,
        )
        pressure_score = _account_routing_candidate_score(
            pressure_best,
            incoming_bid,
            allow_pressure=True,
            model=model,
            max_tokens=max_tokens,
            now=now,
        )
        if pressure_score < strict_score:
            candidates = pressure_candidates
    if not candidates:
        if api_key is not None and config.API_KEY_ROUTING_MODE == "overflow":
            return _route_to_selected_auth(headers, incoming_bid, api_key)
        return incoming_bid, None
    selected = min(
        candidates,
        key=lambda acct: _account_routing_candidate_score(
            acct,
            incoming_bid,
            allow_pressure=True,
            model=model,
            max_tokens=max_tokens,
            now=now,
        ),
    )
    return _route_to_selected_auth(headers, incoming_bid, selected)


def _attempt_for_request(
    request: web.Request, bid: str, cid: str, via: str, model_label: str
) -> _Attempt:
    attempt = _Attempt()
    attempt.started_at = time.time()
    attempt.context = {
        "method": request.method,
        "bid": bid,
        "cid": cid,
        "via": via,
        "model": model_label,
    }
    return attempt


def _record_closed_before_dispatch(path: str, where: str, attempt: _Attempt) -> web.Response:
    return _record_disconnect(
        path,
        where,
        ConnectionResetError("client transport closed before upstream dispatch"),
        attempt,
    )


def _record_early_disconnect_metrics(
    request: web.Request, model_label: str, attempt: _Attempt
) -> None:
    M_REQUESTS.labels(method=request.method, status="499", model=model_label).inc()
    if attempt.started_at is not None:
        M_DURATION.labels(model_label).observe(time.time() - attempt.started_at)


def _disconnect_before_forward(
    request: web.Request,
    path: str,
    where: str,
    bid: str,
    cid: str,
    via: str,
    model_label: str,
    exc: BaseException | None = None,
) -> web.Response:
    """Record a client disconnect before any upstream/central capacity is spent."""
    attempt = _attempt_for_request(request, bid, cid, via, model_label)
    if exc is None:
        response = _record_closed_before_dispatch(path, where, attempt)
    else:
        response = _record_disconnect(path, where, exc, attempt)
    _record_early_disconnect_metrics(request, model_label, attempt)
    return response


def _log_request_start(
    request: web.Request,
    path: str,
    bid: str,
    cid: str,
    via: str,
    model_label: str,
    max_tokens: int | None = None,
    priority: bool = False,
) -> None:
    log(
        f"start  method={request.method} path=/{path} bid={bid} cid={cid} "
        f"via={via} model={model_label} max_tokens={max_tokens} "
        f"lane={'priority' if priority else 'normal'} "
        f"inflight={state['inflight']} queued={state['queued']}"
    )


async def _try_forward(
    request: web.Request,
    headers: Mapping[str, str],
    body: bytes | None,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
    attempt: _Attempt,
    retryable_statuses: set[int] | None = None,
) -> tuple[web.StreamResponse | None, Exception | None]:
    """One ``_forward_once`` call, recording success bookkeeping into ``attempt``.

    Returns ``(response, None)`` on success (and increments ``served``), or
    ``(None, exc)`` on an upstream error so the caller can decide whether to
    retry. Client-side disconnects propagate as exceptions to the caller.
    """
    response, status, captured, exc, meta = await _forward_once(
        request, headers, body, url, client_timeout, retryable_statuses
    )
    if captured is not None:
        attempt.captured = captured
    if meta is not None:
        attempt.meta = meta
    if exc is None:
        state["served"] += 1
        attempt.final_status = status
        attempt.response = response
        return response, None
    return None, exc


def _maybe_fast_fail_throttle_direct(
    bid: str,
    path: str,
    response: web.StreamResponse,
    attempt: _Attempt,
) -> web.Response | None:
    """Return a fast-fail 429/401 for a throttle status on the direct-fallback path."""
    if not (bid and not response.prepared and attempt.final_status in THROTTLE_STATUSES):
        return None
    pause, _ = _pushback_pause(attempt.meta, bid)
    return _retry_after_fast_fail_response(bid, path, pause, source="direct-fallback")


async def _retry_direct_once(
    request: web.Request,
    headers: Mapping[str, str],
    body: bytes | None,
    path: str,
    via: str,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
    first_exc: Exception,
    attempt: _Attempt,
    bid: str = "",
) -> web.StreamResponse | web.Response:
    """One direct retry after the first attempt's upstream error.

    A central failure marks central DOWN and retries against the direct
    upstream; a direct failure retries the same URL once. Returns the streamed
    response on success, a 499 on a client disconnect, or a 502 if the retry
    also fails.
    """
    if via == "central" and isinstance(first_exc, RetryableStatusError) and first_exc.proxy_served:
        # Central itself answered — the 5xx is Anthropic's, relayed (the
        # response carried MARKER_HEADER). Retry direct for THIS request but
        # keep central UP: force-marking DOWN here turned every upstream blip
        # into a fleet-wide stampede past the central semaphore (05/07/2026).
        log(f"central relayed upstream 5xx: {first_exc!r} → central stays up, retrying direct")
        # pick_target would re-pick the still-up central; go direct explicitly.
        retry_url, retry_timeout = direct_target(path, request.query_string)
        retry_where = "during direct-retry"
    elif via == "central":
        log(f"central forward failed: {first_exc!r} → marking DOWN, retrying direct")
        # A real failed request is a stronger signal than a health probe, so we
        # force DOWN immediately (bypassing the probe-fail threshold). Reset the
        # ok streak so recovery still has to clear the OK_THRESHOLD hysteresis.
        state["central_status"] = "down"
        state["central_consecutive_ok"] = 0
        retry_url, retry_timeout, _ = pick_target(path, request.query_string)
        retry_where = "during direct-retry"
    else:
        log(f"upstream-error path=/{path}: {first_exc!r} → retry direct once")
        retry_url, retry_timeout = url, client_timeout
        retry_where = "during upstream-retry"
    state["upstream_retries"] += 1
    M_UPSTREAM_RETRIES.inc()

    lock: asyncio.Lock | None = None
    if via == "central" and config.QUEUE_MODE == "off" and config.CENTRAL_URL:
        # Central failed after this request had already bypassed the local
        # queue. Serialize the emergency direct retry so a central flap cannot
        # turn into N simultaneous direct Anthropic requests.
        lock = _get_direct_fallback_lock()

    try:
        if lock is None:
            response, exc2 = await _try_forward(
                request, headers, body, retry_url, retry_timeout, attempt
            )
        else:
            async with lock:
                response, exc2 = await _try_forward(
                    request, headers, body, retry_url, retry_timeout, attempt
                )
    except _CLIENT_DISCONNECT_EXC as cexc:
        return _record_disconnect(path, retry_where, cexc, attempt)
    if exc2 is None:
        # A direct-fallback throttle response (central down + the account
        # exhausted upstream) skips the pushback loop, so apply the same
        # nudge/fast-fail here — otherwise a stale tab gets a raw multi-day
        # Retry-After 429 instead of the 401 credential re-read nudge. Short
        # retry-afters fall through (fast-fail returns None) unchanged. ``bid``
        # is empty only in unit tests that bypass the nudge wiring.
        if (ff := _maybe_fast_fail_throttle_direct(bid, path, response, attempt)) is not None:
            return ff
        return response
    log(f"upstream-error final path=/{path}: {exc2!r}")
    attempt.final_status = 502
    # Exception CLASS only — the repr already went to the internal log line
    # above; echoing it to the client leaks upstream/central topology detail
    # (py/stack-trace-exposure).
    return web.Response(status=502, text=f"upstream error: {type(exc2).__name__}\n")


def _should_retry_pushback(
    response: web.StreamResponse | web.Response | None,
    attempt: _Attempt,
    pushback_retries: int,
) -> bool:
    """True when an unprepared throttle response is still within the retry budget.

    A relayed queue-wait-timeout 503 is exempt: central's queue is FULL, so a
    pushback retry would only re-park this request against the same saturated
    queue and burn the client's remaining patience — relay it instead so the
    SDK retries on its own clock.
    """
    return (
        response is not None
        and not response.prepared
        and attempt.final_status in THROTTLE_STATUSES
        and pushback_retries < config.RATE_PUSHBACK_RETRIES
        and not _is_queue_timeout_response(response)
    )


# ---------------------------------------------------------------------------
# Spec 092 — SSE keepalive-hold primitives
# ---------------------------------------------------------------------------
#
# For a STREAMING POST /v1/messages that hits a TRANSIENT throttle (529,
# central-queue-depth 503, concurrency 429/503), we commit a "200
# text/event-stream" client response immediately and emit SSE ":" keepalive
# comment frames while internally retrying. The SDK parser silently drops
# SSE comment lines (invariant 3), so the client sees a slow response rather
# than an error banner. The hold consumes the same wait-budget as the queue
# bound so local + central can never stack past client patience (invariant 6).


def _is_streaming_body(body: bytes | None) -> bool:
    """True when the POST body declares ``"stream": true``."""
    if not body:
        return False
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and obj.get("stream") is True


def _is_transient_throttle(
    status: int,
    meta: dict[str, str] | None,
    bid: str,
    response: web.StreamResponse | web.Response | None = None,
) -> bool:
    """True when this throttle is TRANSIENT (hold candidate), not BUDGET.

    Transient = 529 upstream-overloaded, a central-queue-timeout 503, or a
    concurrency/rate 429/503 whose unified budget windows are still "allowed"
    with low utilization. Budget = any of: unified status "allowed_warning" /
    "rejected", binding utilization >= UTILIZATION_WARN, a real Retry-After
    (long windows are budget soft-throttles not concurrency blips).

    Preserves existing invariants:
    * 529 never AIMD-shrinks (OVERLOAD_STATUSES) — we DO hold on 529.
    * Central-queue-timeout 503 is exempt from AIMD and pushback-retry —
      we DO hold (give upstream time to drain), but must not AIMD-shrink.
    * Budget-rejected / long Retry-After / non-streaming → NOT held.
    """
    if status not in THROTTLE_STATUSES:
        return False
    # Central-queue-depth 503: always transient (central queue is full, will
    # drain) — the hold is exactly the banner-killer for it. Its Retry-After is
    # admission backpressure, not a defined upstream window, so it does NOT shed.
    if _is_queue_timeout_response(response):
        return True
    # A real Retry-After beyond the hold ceiling is a DEFINED back-off window,
    # not a momentary blip — shed it (the SDK retries the whole request later)
    # rather than commit a doomed keepalive hold that would only run to the
    # budget deadline and error out. This now covers 529 too: an overloaded
    # upstream asking for a long window will not clear within the client's
    # budget (Codex panel MAJOR — 529 + long Retry-After was always held).
    retry_after = _parse_retry_after(meta)
    if retry_after > config.MAX_HOLD_RETRY_AFTER_S:
        return False
    # 529 = transient (Anthropic capacity, not our usage) once past the RA gate.
    if status in OVERLOAD_STATUSES:
        return True
    # Remaining: ambiguous 429/503. Use _budget_under_pressure to distinguish
    # concurrency (transient) from budget soft-throttle (hold would be wrong).
    return not _budget_under_pressure(meta, bid)


async def _emit_keepalive_frames(response: web.StreamResponse, interval_ms: int) -> None:
    """Emit SSE comment keepalive frames until cancelled.

    Writes ``: keepalive\n\n`` every ``interval_ms`` milliseconds. The
    Anthropic SDK parser silently drops SSE comment lines (spec 092 invariant 3)
    so the client receives heartbeats that reset its idle timer without seeing
    any message content. Must be cancelled from the outside (via Task.cancel).

    07/07 footgun guard: interval must be < the client's idle timeout (~60 s).
    The caller is responsible for passing a sane interval (MIN 500 ms enforced
    in config). This coroutine trusts the caller-validated value.
    """
    interval_s = max(0.5, interval_ms / 1000.0)
    while True:
        await asyncio.sleep(interval_s)
        try:
            await response.write(b": keepalive\n\n")
        except (ConnectionResetError, aiohttp.ClientConnectionResetError, OSError):
            # Client disconnected while we were holding; stop silently.
            return


async def _emit_sse_error_terminal(
    response: web.StreamResponse,
    message: str,
    error_type: str = "throttle_timeout",
) -> None:
    """Write a terminal SSE error event + EOF to an already-prepared response.

    Spec 092 invariant 4: once a 200 SSE response is .prepared, an HTTP error
    status can no longer be sent — the bound-exhausted tail MUST degrade to a
    well-formed SSE error event, never a bare socket close. Callers rely on
    this for the 07/07 falsification: no truncated write.
    """
    import json as _json_mod

    payload = _json_mod.dumps({"type": "error", "error": {"type": error_type, "message": message}})
    try:
        await response.write(f"event: error\ndata: {payload}\n\n".encode())
        await response.write_eof()
    except (ConnectionResetError, aiohttp.ClientConnectionResetError, OSError):
        # Client already gone; write_eof on a dead socket is expected.
        pass


async def _forward_once_into_sse(
    request: web.Request,
    headers: dict,
    body: bytes | None,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
    sse_resp: web.StreamResponse,
    cancel_keepalive: Callable[[], Awaitable[None]] | None = None,
) -> tuple[int, dict[str, str] | None, bytearray | None, Exception | None]:
    """Forward one attempt; on a 2xx, pipe upstream chunks into ``sse_resp``.

    Unlike ``_try_forward`` / ``_stream_response``, this does NOT call
    ``response.prepare(request)`` because ``sse_resp`` is already prepared.
    Returns ``(status, meta, captured, exc)``; ``exc`` non-None means upstream
    network error (not a throttle status — those return normally with their
    status codes). Client-side disconnects raise ``_CLIENT_DISCONNECT_EXC``
    for the caller to handle.

    ``cancel_keepalive`` MUST be awaited the instant a 2xx is confirmed and
    BEFORE the first body chunk is written — otherwise the keepalive emitter and
    the real upstream body race on ``sse_resp.write()`` and a ``: keepalive``
    comment lands mid-frame, corrupting any generation that streams past one
    keepalive interval (the common case for long Opus responses). Cancelling
    here (not in the caller after the pipe) is the ONLY safe ordering because
    only this function knows, per attempt, whether the status is 2xx (pipe →
    cancel first) or a throttle (return → keep the emitter running for the next
    retry).
    """
    from .pacing import _pace_dispatch
    from .ratelimit import _extract_ratelimit, _extract_zai_ratelimit_from_body

    connector = aiohttp.TCPConnector(ssl=True)
    try:
        async with aiohttp.ClientSession(
            timeout=client_timeout, connector=connector, auto_decompress=False
        ) as session:
            await _pace_dispatch()
            try:
                async with session.request(
                    request.method, url, headers=headers, data=body, allow_redirects=False
                ) as upstream:
                    meta = _extract_ratelimit(upstream.headers)
                    # Throttle / error status: return body as captured, no piping.
                    if upstream.status in config.THROTTLE_STATUSES or upstream.status >= 400:
                        upstream_body = await upstream.read()
                        meta.update(
                            _extract_zai_ratelimit_from_body(
                                upstream_body,
                                quota_jitter_s=config.ZAI_QUOTA_RESET_JITTER_S,
                            )
                        )
                        # Surface the proxy-private queue-timeout marker to the
                        # caller so the hold can classify a relayed central
                        # queue-timeout 503 (no AIMD shrink — invariant 7).
                        # _extract_ratelimit does NOT capture this header, so add
                        # it explicitly — and ONLY from a sibling proxy tier
                        # (MARKER_HEADER present), never a spoofing upstream
                        # (anti-spoof, matching _stream_response; Codex BLOCKER).
                        if (
                            config.MARKER_HEADER in upstream.headers
                            and config.QUEUE_TIMEOUT_HEADER in upstream.headers
                        ):
                            meta[config.QUEUE_TIMEOUT_HEADER] = upstream.headers[
                                config.QUEUE_TIMEOUT_HEADER
                            ]
                        captured = bytearray(upstream_body[: 1024 * 1024])
                        return upstream.status, meta, captured, None
                    # 2xx: stop the keepalive emitter BEFORE the first body byte
                    # so it can never interleave a `: keepalive` comment into the
                    # real SSE frames, then pipe chunks into the prepared sse_resp.
                    if cancel_keepalive is not None:
                        await cancel_keepalive()
                    captured = bytearray()
                    cap_limit = 1024 * 1024
                    async for chunk in upstream.content.iter_any():
                        if not chunk:
                            break
                        await sse_resp.write(chunk)
                        if len(captured) < cap_limit:
                            captured.extend(chunk[: cap_limit - len(captured)])
                    await sse_resp.write_eof()
                    return upstream.status, meta, captured, None
            except aiohttp.ClientConnectionResetError:
                raise
            except (TimeoutError, aiohttp.ClientError) as exc:
                return -1, None, None, exc
    except aiohttp.ClientConnectionResetError:
        raise


async def _keepalive_hold_and_retry(
    request: web.Request,
    headers: dict,
    body: bytes | None,
    path: str,
    via: str,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
    attempt: _Attempt,
    bid: str,
    limiter: FairBearerLimiter,
    wait_deadline: float | None,
    first_status: int,
) -> web.StreamResponse:
    """Keepalive-hold path for TRANSIENT streaming throttle (spec 092 T002).

    Prepares a 200 text/event-stream client response immediately, starts the
    keepalive emitter, and internally retries until upstream returns 200 (then
    pipes the real body through the open response) or the wait-budget exhausts
    (then emits a terminal SSE error event — invariant 4, no socket close).

    OWNS the response lifetime: prepares, drains, and writes EOF before
    returning. The caller must return the resulting response without writing.

    Design note on the two-response problem: ``_stream_response`` always calls
    ``response.prepare(request)``. Since ``sse_resp`` is already prepared here,
    we cannot use ``_try_forward`` / ``_forward_once`` for the success retry
    path — they would try to prepare a second response on the same request and
    raise. ``_forward_once_into_sse`` pipes chunks directly into ``sse_resp``
    bypassing the prepare step.
    """
    sse_resp = web.StreamResponse(
        status=200,
        headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
    )
    await sse_resp.prepare(request)

    interval_ms = config.KEEPALIVE_INTERVAL_MS
    log(
        f"keepalive-hold bid={bid} status={first_status} path=/{path} "
        f"interval_ms={interval_ms} deadline={wait_deadline!r}"
    )

    keepalive_task = asyncio.create_task(_emit_keepalive_frames(sse_resp, interval_ms))
    _background_tasks.add(keepalive_task)
    keepalive_task.add_done_callback(_background_tasks.discard)

    async def _await_keepalive_cancel() -> None:
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass
        except Exception as ka_err:
            # The emitter died with a real error, not just our cancel. Record it,
            # but NEVER let it propagate out of cleanup: this runs first in the
            # broad exception handler below, so a re-raise here would skip the
            # terminal-SSE emit and leave the client a truncated 200 (Codex
            # round-3 MAJOR — the same post-prepare footgun, one level down).
            log(f"keepalive-emitter-error bid={bid}: {ka_err!r}")

    try:
        # The throttle that triggered the hold is a real throttle event: apply
        # its AIMD once (canonical _aimd_feedback — 529 + a marked queue-timeout
        # never shrink, a concurrency 429/503 shrinks), then take ownership so
        # _finalize skips its own _aimd_feedback (no double-apply on the
        # terminal). Guarded so a metrics error cannot kill the hold, and kept
        # INSIDE the outer try so ANY raise still hits the terminal-SSE +
        # task-cancel safety below — never a leaked emitter or truncated 200
        # (Codex round-2 MAJOR).
        attempt.aimd_owned = True
        try:
            await _aimd_feedback(bid, limiter, attempt)
        except Exception as origin_aimd_err:
            log(f"keepalive-hold origin-aimd-error bid={bid}: {origin_aimd_err!r}")
        while True:
            now = time.time()
            if wait_deadline is not None and now >= wait_deadline:
                # Budget exhausted — spec 092 invariant 4: emit error SSE, not
                # a bare socket close. The 07/07 falsification test checks this.
                await _await_keepalive_cancel()
                M_KEEPALIVE_HOLDS.labels(outcome="errored").inc()
                log(f"keepalive-hold-exhausted bid={bid} path=/{path}")
                await _emit_sse_error_terminal(
                    sse_resp,
                    "proxy keepalive hold exhausted; upstream capacity unavailable within budget",
                )
                attempt.final_status = 503
                attempt.response = sse_resp
                return sse_resp

            # Honor any outstanding Retry-After, but NEVER sleep past the
            # end-to-end deadline: the hold must not hold the fair slot past the
            # client's wait-budget (invariant 6 — Codex + Opus panel MAJOR).
            # Keepalives keep flowing during this sleep; the next loop iteration
            # re-checks the deadline and exits with a clean SSE error if reached.
            ra_remaining = limiter.retry_after_remaining()
            if ra_remaining > 0:
                budget_left = (
                    (wait_deadline - time.time()) if wait_deadline is not None else ra_remaining
                )
                await asyncio.sleep(max(0.0, min(ra_remaining, budget_left)))

            # Stamp the remaining budget on central-bound requests.
            send_headers: dict = dict(headers)
            if via == "central" and wait_deadline is not None:
                remaining_ms = max(0, int((wait_deadline - time.time()) * 1000))
                send_headers[config.WAIT_BUDGET_HEADER] = str(remaining_ms)

            try:
                status, meta, captured, exc = await _forward_once_into_sse(
                    request,
                    send_headers,
                    body,
                    url,
                    client_timeout,
                    sse_resp,
                    cancel_keepalive=_await_keepalive_cancel,
                )
            except _CLIENT_DISCONNECT_EXC:
                # Client disconnected during our retry.
                await _await_keepalive_cancel()
                attempt.final_status = 499
                attempt.response = sse_resp
                return sse_resp

            if exc is not None:
                # Network-level error; retry after a brief pause.
                log(f"keepalive-hold net-error bid={bid}: {exc!r}")
                await asyncio.sleep(0.5)
                continue

            # Update attempt with the latest meta/captured.
            if meta is not None:
                attempt.meta = meta
            if captured is not None:
                attempt.captured = captured

            if 200 <= status < 300:
                # _forward_once_into_sse already piped the body and called
                # write_eof. The hold succeeded.
                await _await_keepalive_cancel()
                M_KEEPALIVE_HOLDS.labels(outcome="streamed").inc()
                log(f"keepalive-hold-streamed bid={bid} status={status}")
                attempt.final_status = status
                attempt.response = sse_resp
                return sse_resp

            # Build a minimal response object so _is_queue_timeout_response
            # can inspect the headers from the new attempt.
            retry_resp_headers = {}
            if meta and config.QUEUE_TIMEOUT_HEADER in meta:
                retry_resp_headers[config.QUEUE_TIMEOUT_HEADER] = "1"
            retry_fake = web.Response(status=status, headers=retry_resp_headers)
            attempt.final_status = status
            attempt.response = retry_fake

            if _is_transient_throttle(status, meta, bid, retry_fake):
                # Still transient: the canonical _aimd_feedback already does the
                # right thing per status (529 + a marked central queue-timeout
                # never shrink — invariants 7, 9; a concurrency 429/503 shrinks).
                # attempt already reflects this retry's status/meta/response, and
                # retry_fake carries the anti-spoof-gated queue-timeout marker.
                await _aimd_feedback(bid, limiter, attempt)
            else:
                # Reclassified as BUDGET mid-hold (unified headers updated).
                # Exit the hold and emit terminal error.
                await _await_keepalive_cancel()
                M_KEEPALIVE_HOLDS.labels(outcome="errored").inc()
                log(f"keepalive-hold-budget-reclassified bid={bid} path=/{path}")
                await _emit_sse_error_terminal(
                    sse_resp,
                    "proxy keepalive hold stopped: upstream budget exhausted",
                )
                attempt.final_status = status
                attempt.response = sse_resp
                return sse_resp

    except asyncio.CancelledError:
        # Client/loop cancellation — stop the emitter and propagate.
        await _await_keepalive_cancel()
        raise
    except Exception as hold_err:
        # We already prepared a 200 SSE, so an HTTP error can no longer be
        # returned. Stop the emitter FIRST (never race the terminal write),
        # then emit a well-formed terminal SSE error + clean EOF so the client
        # gets a clean close, never a truncated write / phantom 401 (07/07;
        # Codex round-2 MAJOR).
        await _await_keepalive_cancel()
        log(f"keepalive-hold internal-error bid={bid}: {hold_err!r}")
        try:
            await _emit_sse_error_terminal(sse_resp, "proxy keepalive hold internal error")
        except Exception as term_err:
            # The client transport is already gone — the terminal write is best
            # effort; record why it failed rather than swallow it silently.
            log(f"keepalive-hold terminal-emit-failed bid={bid}: {term_err!r}")
        M_KEEPALIVE_HOLDS.labels(outcome="errored").inc()
        attempt.final_status = 503
        attempt.response = sse_resp
        return sse_resp
    finally:
        # Belt-and-suspenders: the emitter must NEVER outlive this call (leaked
        # task / SSE dribble to a dead client). Every normal exit already awaited
        # its cancel; this covers any path that did not.
        if not keepalive_task.done():
            keepalive_task.cancel()


async def _forward_with_retry(
    request: web.Request,
    headers: Mapping[str, str],
    body: bytes | None,
    path: str,
    via: str,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
    attempt: _Attempt,
    bid: str,
    limiter: FairBearerLimiter,
    wait_deadline: float | None = None,
) -> web.StreamResponse | web.Response:
    """Forward once, then retry direct on upstream error. Behavior-identical to
    the original inline chain: a central failure marks central DOWN and retries
    direct; a direct failure retries direct once; client disconnects yield 499.

    ``wait_deadline`` (epoch seconds) is the end of this request's queue-wait
    budget. Every CENTRAL attempt is stamped with the budget REMAINING at send
    time — stamping once before the loop let a pushback-retry sleep here and
    then re-grant central the original full window, reopening the >60 s
    silence (Codex round-2 BLOCKER on PR #83). Direct sends to the raw
    upstream never carry the proxy-private header.
    """
    pushback_retries = 0
    while True:
        send_headers = headers
        if via == "central" and wait_deadline is not None:
            send_headers = dict(headers)
            send_headers[config.WAIT_BUDGET_HEADER] = str(
                max(0, int((wait_deadline - time.time()) * 1000))
            )
        try:
            response, exc = await _try_forward(
                request,
                send_headers,
                body,
                url,
                client_timeout,
                attempt,
                retryable_statuses={500, 502, 504} if via == "central" else None,
            )
        except _CLIENT_DISCONNECT_EXC as cexc:
            return _record_disconnect(path, "first", cexc, attempt)
        if exc is not None:
            return await _retry_direct_once(
                request, headers, body, path, via, url, client_timeout, exc, attempt, bid
            )
        # Spec 092 keepalive-hold FIRST — BEFORE the legacy pushback-retry. With
        # the default RATE_PUSHBACK_RETRIES=1 the pushback branch's silent
        # `wait_retry_after` would otherwise run on the FIRST transient throttle,
        # leaving the client in silence with no SSE committed and no keepalive —
        # the exact 07/07 truncated-write class this feature exists to prevent
        # (Codex panel BLOCKER). For an eligible streaming + transient throttle,
        # engage the hold now; it owns its own retries WITH keepalives and its
        # own AIMD. Non-streaming / budget / no-wait-budget throttles fall
        # through to the unchanged pushback path below.
        #   1. KEEPALIVE_HOLD enabled
        #   2. response is an unprepared throttle (can still upgrade to 200 SSE)
        #   3. streaming POST /v1/messages (stream:true)
        #   4. a wait-budget exists and is not yet spent
        #   5. the throttle is TRANSIENT not budget (checked LAST — most costly)
        if (
            config.KEEPALIVE_HOLD
            and response is not None
            and not response.prepared
            and attempt.final_status in THROTTLE_STATUSES
            and _is_streaming_body(body)
            and wait_deadline is not None
            and time.time() < wait_deadline
            and _is_transient_throttle(attempt.final_status, attempt.meta, bid, response)
        ):
            _schedule_advisor(bid, attempt.final_status)
            return await _keepalive_hold_and_retry(
                request,
                dict(headers),
                body,
                path,
                via,
                url,
                client_timeout,
                attempt,
                bid,
                limiter,
                wait_deadline,
                attempt.final_status,
            )
        if _should_retry_pushback(response, attempt, pushback_retries):
            pushback_retries += 1
            retry_after = _parse_retry_after(attempt.meta)
            pause, synthetic_pause = _pushback_pause(attempt.meta, bid)
            log(
                f"rate-pushback-retry bid={bid} status={attempt.final_status} "
                f"retry={pushback_retries}/{config.RATE_PUSHBACK_RETRIES} "
                f"pause={pause} retry_after={retry_after} synthetic_pause={synthetic_pause}"
            )
            if (
                fast_fail := _retry_after_fast_fail_response(bid, path, pause, source="pushback")
            ) is not None:
                return fast_fail
            await _aimd_feedback(bid, limiter, attempt)
            _schedule_advisor(bid, attempt.final_status)
            await limiter.wait_retry_after()
            continue
        return response


def _budget_under_pressure(meta: Mapping[str, str] | None, bid: str = "") -> bool:
    """True when the OAuth unified windows say a 429 is BUDGET, not concurrency.

    Anthropic returns 429-without-Retry-After for two very different reasons:
    a 5h/7d budget soft-throttle, and a per-account concurrency / rate cap.
    They are told apart by the ``anthropic-ratelimit-unified-*`` headers on the
    same response — a budget throttle shows ``allowed_warning``/``rejected`` (or
    the binding window's utilization at/over the warn line), while a pure
    concurrency 429 arrives while every window is still ``allowed`` with low
    utilization.

    A concurrency/rate 429 frequently arrives WITHOUT unified headers on its own
    response. Defaulting straight to "budget" there re-opens the concentration
    incident from the other side: an account that is 91 % weekly-empty gets the
    full ``AIMD_BACKOFF_S`` collapse because one burst tripped its concurrency
    cap (05/07/2026 — fresh account absorbed the whole fleet, u7d=0.09,
    unified-status=allowed, yet every headerless 429 read as budget → cap
    collapsed to 1 → queue → disconnects). So when the 429 itself carries no
    unified headers, fall back to the bearer's CACHED unified state (updated on
    every response that DOES carry them, ``bearer_state[bid]["unified"]``): a
    real budget wall shows ``allowed_warning``/``rejected`` there before it ever
    424s, so the cache reliably distinguishes the two. Only when neither the
    response nor the cache has unified data (API-key traffic) → assume budget so
    the historical conservative backoff is preserved.
    """
    unified = _parse_unified(meta)
    if not unified and bid:
        # ``.get(bid)`` not ``[bid]``: never KeyError if classification runs for
        # a bearer not yet in bearer_state (defensive — the hot path always
        # initializes it first, but the helper must not assume that).
        bstate = bearer_state.get(bid) or {}
        cached = bstate.get("unified")
        cached_at = bstate.get("unified_at")
        # Trust the cache ONLY while fresh: a budget wall stops producing the
        # header-bearing successes that refresh it, so a stale sample must NOT
        # keep a walled account classified as concurrency (adversarial review
        # 05/07/2026). Stale/absent → fall through to the conservative default.
        if (
            cached
            and isinstance(cached_at, (int, float))
            and (time.time() - cached_at) <= UNIFIED_CACHE_FRESH_S
        ):
            unified = cached
    if not unified:
        return True
    statuses = (unified.get("status"), unified.get("status_5h"), unified.get("status_7d"))
    if any(s in ("allowed_warning", "rejected") for s in statuses):
        return True
    binding = _binding_utilization(unified)
    if binding is None:
        return True
    return binding >= UTILIZATION_WARN


def _pushback_pause(meta: Mapping[str, str] | None, bid: str = "") -> tuple[float, bool]:
    """Return the pause seconds and whether it was synthesized locally.

    A real ``Retry-After`` always wins. Without one, classify by the unified
    budget headers (falling back to the bearer's cached unified state when the
    429 itself omits them — see ``_budget_under_pressure``): a genuine budget
    soft-throttle gets the full AIMD cooldown (hold until the window eases), but
    a concurrency/rate 429 gets only a short ``CONCURRENCY_COOLDOWN_S`` — the
    AIMD shrink already sheds the load and the 429 clears the instant inflight
    drops, so a 30s pause would needlessly collapse the active account to cap=1
    and hold it there under fleet load.
    """
    retry_after = _parse_retry_after(meta)
    if retry_after > 0:
        return retry_after, False
    if _budget_under_pressure(meta, bid):
        return max(0.0, config.AIMD_BACKOFF_S), True
    return max(0.0, config.CONCURRENCY_COOLDOWN_S), True


def _note_retry_after_if_set(
    limiter: FairBearerLimiter, meta: Mapping[str, str] | None, bid: str = ""
) -> tuple[float, bool]:
    """Apply pushback pause to limiter when one is set; return (pause, synthetic_pause)."""
    pause, synthetic_pause = _pushback_pause(meta, bid)
    if pause > 0:
        limiter.note_retry_after(pause)
    return pause, synthetic_pause


# Cache of the active-account credential's bearer id, keyed by (mtime_ns, size)
# so the common case is one stat() per fast-fail decision.
_active_bearer_cache: tuple[int, int, str] | None = None


def _active_account_bearer() -> str:
    """Bearer id of the fleet's current active credential, or '' when disabled.

    ``config.ACTIVE_CRED_PATH`` (``THROTTLE_ACTIVE_CRED_PATH``) names the single
    credential file every tab reads under the single-active-account failover
    model. A captive broker swaps it between accounts on a 7d limit; comparing
    an exhausted request's bearer against this is what lets the proxy 401-nudge
    a stale tab into re-reading the swapped file. The token is hashed exactly as
    ``_bearer_id`` hashes the Authorization header and immediately dropped
    (invariant #2). mtime/size cached; returns '' on any miss so the caller
    falls back to the historical fast-fail.
    """
    path = config.ACTIVE_CRED_PATH
    if not path:
        return ""
    global _active_bearer_cache
    try:
        st = os.stat(path)
    except OSError:
        _active_bearer_cache = None
        return ""
    key = (st.st_mtime_ns, st.st_size)
    cached = _active_bearer_cache
    if cached is not None and cached[:2] == key:
        return cached[2]
    try:
        with open(path, encoding="utf-8") as fh:
            token = (json.load(fh).get("claudeAiOauth") or {}).get("accessToken")
    except (OSError, ValueError, AttributeError):
        _active_bearer_cache = None
        return ""
    if not isinstance(token, str) or not token:
        _active_bearer_cache = None
        return ""
    bid = hashlib.sha256(f"Bearer {token}".encode("utf-8", "replace")).hexdigest()[:8]
    _active_bearer_cache = (key[0], key[1], bid)
    return bid


def _credential_nudge_response(bid: str, path: str, source: str) -> web.Response:
    """Local 401 that triggers claude's credential re-read (see ACTIVE_CRED_PATH).

    Returned INSTEAD of the long-Retry-After 429 when the fleet's active account
    was swapped out from under a still-running tab. The body mirrors an Anthropic
    auth error so the client's 401 self-heal fires; it carries no Retry-After (a
    401 means "re-read your creds", not "back off"). Disabled when account
    routing is enabled, because the router already selects the upstream bearer
    per request.
    """
    M_CREDENTIAL_NUDGE.labels(bearer=bid).inc()
    log(f"credential-nudge bid={bid} path=/{path} source={source}")
    return web.Response(
        status=401,
        content_type="application/json",
        text=(
            '{"type":"error","error":{"type":"authentication_error",'
            '"message":"throttle-proxy: active account changed; re-read credentials"}}'
        ),
    )


def _retry_after_fast_fail_response(
    bid: str,
    path: str,
    remaining_s: float,
    *,
    source: str,
) -> web.Response | None:
    """Return a local 401 nudge or 429 when the Retry-After window is too long.

    When ``ACTIVE_CRED_PATH`` is set and the active credential's bearer differs
    from this exhausted request's bearer, the fleet has failed over to the other
    account and this tab is stale: return a 401 so claude re-reads the swapped
    credential and adopts the live account (no restart). Otherwise fall back to
    the historical 429 fast-fail. Once a tab adopts the new token its bearer
    matches the active one, so no further nudge fires — no loop, and if BOTH
    accounts are exhausted the 429 is the correct answer.
    """
    if remaining_s <= config.MAX_HOLD_RETRY_AFTER_S:
        return None
    active = "" if _account_routing_enabled() else _active_account_bearer()
    if active and active != bid:
        return _credential_nudge_response(bid, path, source)
    retry_after_s = max(1, math.ceil(remaining_s))
    log(
        f"retry-after-fast-fail bid={bid} path=/{path} source={source} "
        f"remaining={retry_after_s} max_hold={config.MAX_HOLD_RETRY_AFTER_S}"
    )
    return web.Response(
        status=429,
        headers={"retry-after": str(retry_after_s)},
        text=(
            "upstream retry-after window is active; failing fast instead "
            "of holding the local gateway request\n"
        ),
    )


async def _aimd_feedback(bid: str, limiter: FairBearerLimiter, attempt: _Attempt) -> None:
    """Apply AIMD shrink/overload/grow + Retry-After feedback for one request."""
    final_status = attempt.final_status
    retry_after = _parse_retry_after(attempt.meta)
    if final_status in AIMD_STATUSES and _is_queue_timeout_response(attempt.response):
        # A relayed queue-wait-timeout 503 is central admission backpressure,
        # not upstream pushback on this bearer: shrinking here would
        # misattribute central's queue depth to the bearer's own upstream
        # behavior and collapse a healthy local cap.
        log(f"queue-timeout-relay bid={bid} status={final_status} (no aimd shrink)")
        return
    if final_status in AIMD_STATUSES and _is_zai_quota_gate(attempt.meta):
        # Z.ai 1316/1317/1308 mean the plan window is exhausted. That is a
        # quota gate, not evidence that the current concurrency ceiling is too
        # high, so hold admission until the body reset instead of AIMD shrinking.
        pause, synthetic_pause = _note_retry_after_if_set(limiter, attempt.meta, bid)
        code = (attempt.meta or {}).get("zai-error-code", "unknown")
        reset = (attempt.meta or {}).get("zai-reset-epoch", "unknown")
        log(
            f"quota-gate bid={bid} provider=zai status={final_status} code={code} "
            f"retry_after={retry_after} pause={pause} reset_epoch={reset} "
            f"synthetic_pause={synthetic_pause} (no aimd shrink)"
        )
        return
    if final_status in AIMD_STATUSES:
        # Rate pushback (429/503) → multiplicative-decrease so future requests
        # queue here instead of being slammed against Anthropic's counter.
        new_max = await limiter.shrink()
        M_AIMD_SHRINKS.labels(bearer=bid, status=str(final_status)).inc()
        if new_max is not None:
            M_AIMD_MAX.labels(bearer=bid).set(new_max)
        pause, synthetic_pause = _note_retry_after_if_set(limiter, attempt.meta, bid)
        log(
            f"aimd-shrink bid={bid} status={final_status} "
            f"max_concurrent={new_max} retry_after={retry_after} "
            f"pause={pause} synthetic_pause={synthetic_pause}"
        )
    elif final_status in OVERLOAD_STATUSES:
        # 529 = upstream overloaded (not our usage): honor any retry-after but
        # do NOT shrink the ceiling.
        M_AIMD_OVERLOAD.labels(bearer=bid).inc()
        pause, synthetic_pause = _note_retry_after_if_set(limiter, attempt.meta, bid)
        log(
            f"overload bid={bid} status={final_status} retry_after={retry_after} "
            f"pause={pause} synthetic_pause={synthetic_pause} (no shrink)"
        )
    elif final_status and 200 <= final_status < 400:
        new_max = await limiter.grow()
        if new_max is not None:
            M_AIMD_GROWS.labels(bearer=bid).inc()
            M_AIMD_MAX.labels(bearer=bid).set(new_max)
            log(f"aimd-grow bid={bid} max_concurrent={new_max}")


def _record_usage(model: str, model_label: str, captured: bytearray, path: str) -> None:
    """Parse the SSE usage block and bump token/cost metrics (POST /v1/messages)."""
    try:
        usage = _parse_sse_usage(bytes(captured))
        rates = _pricing_for(model)
        for kind, count in usage.items():
            if count <= 0:
                continue
            M_TOKENS.labels(model=model_label, kind=kind).inc(count)
            cost = (count / 1_000_000.0) * rates[kind]
            M_COST.labels(model=model_label, kind=kind).inc(cost)
    except Exception as ue:
        log(f"usage-parse-error path=/{path}: {ue!r}")


def _log_413_reason(bid: str, model_label: str, captured: bytearray | None) -> None:
    """Surface the actual Anthropic error reason on a 413 response.

    claude-code's TUI hard-codes "Request too large (max 32MB)" for every
    413 status, but Anthropic's 413 has a JSON body with the real cause:
    ``prompt is too long`` (token-count cap), ``input messages exceed
    maximum allowed`` (per-message), ``the beta header ...`` (combination
    rejected), and so on. PR #17 already showed bodies under 1 MiB getting
    413'd — so the 32 MiB byte cap is a red herring; this log pins the
    real bottleneck. Caps response read at 64 KiB to keep the journal
    line bounded for edge-case error envelopes.

    PR #20: handle the empty-captured case explicitly. Observed
    empirically on Pedro's desktop: the central tier sometimes
    forwards a Content-Length:0 413 envelope, so ``captured`` arrives
    here as an empty ``bytearray``. The original guard in ``_finalize``
    short-circuited those, leaving 413s entirely undiagnosed. Log
    `empty_body` instead so the operator at least knows the upstream
    rejected without an error envelope.
    """
    if not captured:
        log(f"upstream_413 bid={bid} model={model_label} reason=empty_body")
        return
    raw = bytes(captured[:65536])
    try:
        err = json.loads(raw)
        outer_err = err.get("error") if isinstance(err.get("error"), dict) else {}
        msg = outer_err.get("message") or err.get("message") or "<no message>"
        etype = outer_err.get("type") or err.get("type") or "<no type>"
        log(f"upstream_413 bid={bid} model={model_label} type={etype!r} message={msg!r}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log(f"upstream_413 bid={bid} model={model_label} parse_error={exc!r} preview={raw[:200]!r}")


def _schedule_advisor(bid: str, final_status: int) -> None:
    """Fire the out-of-band GROQ advisor (debounced) on a throttle status."""
    if ADVISOR_ENABLED and final_status in THROTTLE_STATUSES:
        advisor_task = asyncio.create_task(_maybe_advise(bid, final_status))
        _background_tasks.add(advisor_task)
        advisor_task.add_done_callback(_background_tasks.discard)


async def _finalize(
    counters: _Counters,
    bid: str,
    limiter: FairBearerLimiter,
    bstate: dict[str, object],
    attempt: _Attempt,
    t0: float,
    model: str,
    model_label: str,
    request: web.Request,
    path: str,
) -> None:
    """Run the per-request ``finally`` bookkeeping: counters, gauges, AIMD,
    advisor, and usage parsing. Exactly mirrors the original inline block.
    """
    final_status = attempt.final_status
    counters.exit_inflight(final_status)
    M_REQUESTS.labels(method=request.method, status=str(final_status), model=model_label).inc()
    M_DURATION.labels(model=model_label).observe(time.time() - t0)

    # Capture upstream rate-limit headroom for this bearer.
    meta = attempt.meta
    if meta:
        bstate["last_ratelimit"] = meta
        _publish_ratelimit_gauges(bid, meta)
        try:
            await _apply_unified(bid, bstate, limiter, meta)
        except Exception as ue:
            log(f"unified-error bid={bid}: {ue!r}")

    # The keepalive-hold applies AIMD per-throttle itself; re-applying here would
    # double-shrink and, on an exhausted 529/queue-timeout hold (terminal
    # synthetic 503), wrongly shrink the bearer (invariants 7 + 9 — Codex BLOCKER).
    if not attempt.aimd_owned:
        try:
            await _aimd_feedback(bid, limiter, attempt)
        except Exception as aimde:
            log(f"aimd-error bid={bid}: {aimde!r}")

    _schedule_advisor(bid, final_status)

    if attempt.captured and request.method == "POST" and "v1/messages" in path:
        _record_usage(model, model_label, attempt.captured, path)
    if final_status == 413:
        # PR #19/#20: log Anthropic's 413 response body so the operator
        # can read the actual error reason. claude-code's TUI paraphrases
        # every 413 as "Request too large (max 32MB)" regardless of cause.
        # PR #20 drops the `and attempt.captured` guard — observed
        # empirically that some upstream paths return 413 with an empty
        # body, which made the guard short-circuit and leave the event
        # entirely undiagnosed. _log_413_reason now handles None / empty
        # captured by logging `reason=empty_body`, so every 413 produces
        # at least one diagnostic line.
        _log_413_reason(bid, model_label, attempt.captured)
    log(
        f"done   path=/{path} model={model_label} inflight={counters.s['inflight']} "
        f"queued={counters.s['queued']} served={counters.s['served']} "
        f"disc={counters.s['client_disconnects']} retries={counters.s['upstream_retries']}"
    )
    _maybe_warn_storm(counters.s["upstream_retries"])


def _apply_body_shrink(
    request: web.Request,
    body: bytes,
    path: str,
    model_label: str,
    headers: dict[str, str],
) -> bytes:
    """Trim oversize POST /v1/messages bodies (PR #15) and emit diagnostics.

    Returns the (possibly trimmed) body and refreshes ``headers['Content-Length']``
    in place when it shrinks. Behavior-identical to the former inline block; the
    no-trim ``v1/messages`` case logs a passthrough breadcrumb (PR #17).
    """
    body, shrink_meta = shrink_body(body, path)
    if shrink_meta.get("trimmed"):
        still = "true" if shrink_meta.get("still_oversize") else "false"
        M_BODY_SHRINK_TRIMMED.labels(model=model_label, still_oversize=still).inc()
        M_BODY_SHRINK_BYTES_SAVED.labels(model=model_label).inc(shrink_meta.get("bytes_saved", 0))
        log(
            f"body_shrink bid={_bearer_id(request.headers)} model={model_label} "
            f"original={shrink_meta['original_bytes']} "
            f"final={shrink_meta['final_bytes']} "
            f"blocks_trimmed={shrink_meta['blocks_trimmed']} "
            f"saved={shrink_meta['bytes_saved']} "
            f"still_oversize={shrink_meta['still_oversize']}"
        )
        # When the proxy MUTATES the body we have to refresh Content-Length; the
        # header dict we forward was built from the ORIGINAL request and would
        # lie about the payload size if we left it untouched.
        headers["Content-Length"] = str(len(body))
    elif "v1/messages" in path and shrink_meta.get("original_bytes") is not None:
        reason = shrink_meta.get("reason", "under-cap")
        log(
            f"body_passthrough bid={_bearer_id(request.headers)} "
            f"model={model_label} bytes={shrink_meta['original_bytes']} "
            f"reason={reason}"
        )
    return body


async def handler(request: web.Request) -> web.StreamResponse:
    """Main reverse-proxy handler: queue, forward (with retry), and stream back.

    Acquires a per-bearer fair slot, picks central-or-direct upstream, forwards
    the request, streams the response, and on the way out applies AIMD feedback,
    publishes metrics, fires the optional advisor, and parses SSE usage.
    """
    handler_start = time.time()
    path = request.match_info.get("path", "")
    # The wait-budget header is CONSUMED here (via _effective_queue_max_wait)
    # and re-stamped canonically per forward attempt — passing a client's
    # mixed-case copy through would coexist with the stamped lowercase one,
    # and the next tier's CIMultiDict.get() would read the client's value
    # first, defeating the min() (Codex round-2 BLOCKER on PR #83).
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_HEADERS and k.lower() != config.WAIT_BUDGET_HEADER
    }
    bid = _bearer_id(request.headers)
    cid = _client_id(request)
    url, client_timeout, via = pick_target(path, request.query_string)
    try:
        body = await request.read() if request.body_exists else None
    except _CLIENT_DISCONNECT_EXC as exc:
        return _disconnect_before_forward(request, path, "read-body", bid, cid, via, "unknown", exc)

    # PR #557: extract model from POST /v1/messages body for metrics labels.
    model = _extract_model_from_body(body) if body else ""
    model_label = model or "unknown"

    # Priority lane: a short/latency-sensitive call (the /goal Stop-hook
    # evaluator — small max_tokens, no tools, small body) dispatches from a
    # dedicated reserve pool so it never starves behind long generations.
    # Fail-safe: an unparseable/absent max_tokens stays in the normal lane,
    # and so does any large body — max_tokens caps only the OUTPUT, so a
    # giant no-tools prompt could otherwise jump the queue.
    req_max_tokens, req_has_tools = _short_request_hint(body)
    is_priority = _is_priority_request(req_max_tokens, req_has_tools, len(body or b""))

    if _request_disconnected(request):
        return _disconnect_before_forward(request, path, "pre-queue", bid, cid, via, model_label)

    # PR #15: trim oversize POST /v1/messages bodies before forwarding so we
    # do not hand Anthropic a payload they will reject with the 32MB cap.
    # See body_shrink.py for the algorithm + trade-offs (cache invalidation,
    # breadcrumb stubs, hard floor on single-attachment overruns).
    if body is not None and request.method == "POST":
        body = _apply_body_shrink(request, body, path, model_label, headers)

    bid, _account_label = _route_account_if_enabled(
        headers,
        bid,
        method=request.method,
        path=path,
        model=model,
        max_tokens=req_max_tokens,
    )
    queue_mode, hard_max = _effective_admission(bid)
    # PR #562 chooses the limiter by bearer, so two OAuth tokens get two
    # independent slot pools. PR #573 makes that limiter a FairBearerLimiter,
    # dispatched round-robin per client connection.
    limiter = await _get_bearer_limiter(bid, queue_mode, hard_max)
    if (
        fast_fail := _retry_after_fast_fail_response(
            bid, path, limiter.retry_after_remaining(), source="pre-dispatch"
        )
    ) is not None:
        return fast_fail
    bstate = bearer_state[bid]
    cstate = bstate["clients"].setdefault(cid, {"queued": 0, "inflight": 0, "served": 0})
    counters = _Counters(bid, cid, bstate, cstate)

    max_wait = _effective_queue_max_wait(request.headers)
    if max_wait is not None and max_wait <= 0.0:
        # The upstream tier already spent the whole wait budget; don't park.
        return _queue_wait_timeout_response(bid, cid, path, limiter, max_wait)

    if limiter.queue_enabled:
        counters.enqueue(request, path)
    else:
        log(
            f"queue-bypass method={request.method} path=/{path} bid={bid} "
            f"cid={cid} inflight={state['inflight']} queue_mode={config.QUEUE_MODE}"
        )

    try:
        async with limiter.slot(cid, priority=is_priority, max_wait=max_wait) as held:
            counters.dequeue()
            counters.enter_inflight()
            t0 = time.time()
            attempt = _attempt_for_request(request, bid, cid, via, model_label)
            attempt.started_at = t0
            try:
                if _request_disconnected(request):
                    return _record_closed_before_dispatch(path, "post-queue", attempt)
                # held.priority is the EFFECTIVE lane (reserve 0 or a mid-wait
                # retune can demote) — log that, not the requested one.
                _log_request_start(
                    request, path, bid, cid, via, model_label, req_max_tokens, held.priority
                )
                # Honor any outstanding upstream Retry-After for this bearer before
                # we dispatch — don't spin a request against a known-closed window.
                if (
                    fast_fail := _retry_after_fast_fail_response(
                        bid, path, limiter.retry_after_remaining(), source="post-slot"
                    )
                ) is not None:
                    attempt.final_status = fast_fail.status
                    return fast_fail
                await limiter.wait_retry_after()
                if _request_disconnected(request):
                    return _record_closed_before_dispatch(path, "pre-dispatch", attempt)
                return await _forward_with_retry(
                    request,
                    headers,
                    body,
                    path,
                    via,
                    url,
                    client_timeout,
                    attempt,
                    bid,
                    limiter,
                    # Deadline (not a snapshot): every central attempt re-stamps
                    # the REMAINING budget, so pushback sleeps between retries
                    # keep eating it instead of re-granting central a full
                    # window (Codex round-2 BLOCKER on PR #83).
                    wait_deadline=None if max_wait is None else handler_start + max_wait,
                )
            finally:
                await _finalize(
                    counters,
                    bid,
                    limiter,
                    bstate,
                    attempt,
                    t0,
                    model,
                    model_label,
                    request,
                    path,
                )
    except QueueWaitTimeout:
        # No slot within the wait bound: answer 503 while the client's
        # transport is still open (the limiter already rolled its queue entry
        # back via acquire's cancellation path; no release is owed).
        counters.dequeue()
        return _queue_wait_timeout_response(bid, cid, path, limiter, max_wait or 0.0)
    finally:
        # PR #575 B1 fix: if we got cancelled between incrementing `queued`
        # and the inner `async with limiter.slot()` body decrementing it,
        # roll back the queue counter so it doesn't leak forever and starve
        # the /__throttle/health gauge.
        if counters.queued_incremented:
            counters.dequeue()
            log(f"queue-leak-rollback bid={bid} cid={cid} (cancelled before slot dispatch)")


async def _check_upstream_egress() -> tuple[bool, str]:
    parsed = urlsplit(config.UPSTREAM)
    host = parsed.hostname
    if not host:
        return True, ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=config.UPSTREAM_HEALTH_TIMEOUT,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


# FR-005 distinctness guard: last-warned collision signature, so the health
# poll (every ~5 s) emits ONE log line per distinct collision set instead of
# spamming. Empty = no collision currently warned. "emitted_sus" tracks the
# last verdict signature whose UNVERIFIED warning was actually logged —
# concurrent verifier tasks (different suspected keys) reaching the same
# still-suspected verdict must not both emit it (Codex re-verify MINOR).
_identity_warn_state: dict[str, str] = {"sig": "", "emitted_sus": ""}
# suspected-signature → in-flight verification task (verify-before-warn):
# dedupes the probe while health keeps polling the same suspicion.
_identity_verify_tasks: dict[str, asyncio.Task] = {}
# Gap before the second probe attempt of an unresolved suspicion. Module-level
# so tests can zero it.
_IDENTITY_VERIFY_RETRY_S = 5.0


def _account_identity_verdict() -> dict[str, object] | None:
    """Cheap cross-store identity verdict for the health surface (or None).

    Off the request hot path; each ``guard_email`` is a cache hit except on a
    credential rotation (then one small local read). Returns None when account
    routing/paths are unconfigured so the field stays invisible on the central
    tier. Never raises to the caller — health is load-bearing (invariant #4).
    """
    if not config.ACCOUNT_CRED_PATHS:
        return None
    from . import accounts

    view = []
    for acct in accounts.account_snapshot():
        email, verified = accounts.guard_email(acct["path"])
        view.append({"label": acct["label"], "email": email, "verified": verified})
    return accounts.identity_state(view)


def _identity_sig(verdict: dict[str, object]) -> str:
    """Debounce signature over BOTH verified and suspected collision sets."""
    dup = verdict.get("duplicates") or {}
    sus = verdict.get("suspected") or {}
    return ";".join(
        [f"dup:{email}={','.join(labels)}" for email, labels in sorted(dup.items())]
        + [f"sus:{email}={','.join(labels)}" for email, labels in sorted(sus.items())]
    )


def _emit_identity_warning(groups: dict[str, list[str]], verified: bool) -> None:
    """One collision log line; the unverified variant names its uncertainty."""
    detail = "; ".join(f"{'+'.join(labels)} → {email}" for email, labels in sorted(groups.items()))
    if verified:
        log(
            f"ACCOUNT COLLISION: credential stores share one account — {detail}. "
            "The same account in >1 store rotates one refresh-token family and mutually "
            "revokes it (09/07 outage). Give each store a DISTINCT account."
        )
    else:
        log(
            f"ACCOUNT COLLISION (unverified): credential stores may share one account — "
            f"{detail} — but the profile probe could not confirm it (dead token or "
            "throttled probe). The shared email may also be a stale .claude.json label "
            "(promote credential swap). Verify the stores before re-authing anything."
        )


def _verify_suspected_key(suspected: dict[str, list[str]]) -> str:
    return ";".join(f"{email}={','.join(labels)}" for email, labels in sorted(suspected.items()))


async def _verify_suspected_identity(key: str, suspected: dict[str, list[str]]) -> None:
    """Probe the live tokens behind a SUSPECTED collision before alarming.

    A suspected group carries at least one email that is only a local
    ``.claude.json`` label. Probing ``/api/oauth/profile`` for the unverified
    members either dissolves the group (stale label — the 10/07 promote-swap
    false alarm held for ~2.6 h before this path existed), confirms a real
    verified collision (warn exactly as before), or fails (dead token — warn,
    flagged unverified, so the 09/07 dead-collision class stays detectable).
    """
    from . import accounts

    try:
        flagged = {label for labels in suspected.values() for label in labels}
        paths = {a["label"]: a["path"] for a in accounts.account_snapshot()}
        for attempt in range(2):
            unresolved = [
                paths[lb]
                for lb in sorted(flagged)
                if lb in paths and not accounts.guard_email(paths[lb])[1]
            ]
            if not unresolved:
                break
            if attempt:
                await asyncio.sleep(_IDENTITY_VERIFY_RETRY_S)
            for path in unresolved:
                await accounts.force_verify_email(path)
        verdict = _account_identity_verdict() or {}
        duplicates = verdict.get("duplicates") or {}
        still_suspected = verdict.get("suspected") or {}
        M_ACCOUNT_COLLISIONS.set(sum(len(labels) for labels in duplicates.values()))
        M_ACCOUNT_SUSPECTED.set(sum(len(labels) for labels in still_suspected.values()))
        # Debounce against interleaved emitters (Codex MINOR): a health poll may
        # have already warned this verdict while we slept — only the verifier
        # warns unverified, so that branch is its own once-per-epoch emitter.
        new_sig = _identity_sig(verdict)
        changed = new_sig != _identity_warn_state["sig"]
        _identity_warn_state["sig"] = new_sig
        if duplicates and changed:
            _emit_identity_warning(duplicates, verified=True)
        if still_suspected:
            if _identity_warn_state["emitted_sus"] != new_sig:
                _identity_warn_state["emitted_sus"] = new_sig
                _emit_identity_warning(still_suspected, verified=False)
        else:
            # Resolved (verified or cleared): re-arm the unverified emitter so a
            # LATER re-suspicion (a real new transition) warns again.
            _identity_warn_state["emitted_sus"] = ""
            if not duplicates and changed:
                log(
                    f"account-identity: suspected collision cleared by profile probe ({key})"
                    " — stale .claude.json label (e.g. promote credential swap); stores"
                    " verified distinct."
                )
    except Exception as exc:
        log(f"account-identity verification error (non-fatal): {exc!r}")


def _spawn_identity_verification(suspected: dict[str, list[str]]) -> None:
    key = _verify_suspected_key(suspected)
    task = _identity_verify_tasks.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(_verify_suspected_identity(key, suspected))
    _identity_verify_tasks[key] = task

    def _cleanup(_t: asyncio.Task, key: str = key, task: asyncio.Task = task) -> None:
        # Pop only OUR entry: a stale done-callback firing after a same-key
        # respawn must not evict the newer live task (Codex MAJOR).
        if _identity_verify_tasks.get(key) is task:
            _identity_verify_tasks.pop(key, None)

    task.add_done_callback(_cleanup)


def _note_identity_collision(verdict: dict[str, object] | None) -> None:
    """Gauge + debounced warning on VERIFIED duplicates; probe suspected ones.

    Health must answer in <50 ms (invariant #4), so suspected groups are never
    probed inline — a background task verifies, then warns or clears.
    """
    duplicates = (verdict or {}).get("duplicates") or {}
    suspected = (verdict or {}).get("suspected") or {}
    M_ACCOUNT_COLLISIONS.set(sum(len(labels) for labels in duplicates.values()))
    M_ACCOUNT_SUSPECTED.set(sum(len(labels) for labels in suspected.values()))
    sig = _identity_sig(verdict or {})
    if sig == _identity_warn_state["sig"]:
        return
    _identity_warn_state["sig"] = sig
    if duplicates:
        _emit_identity_warning(duplicates, verified=True)
    if suspected:
        _spawn_identity_verification(suspected)


def _publish_brake_enabled() -> None:
    """Mirror the brake-armed state onto its gauge. Called from BOTH health()
    and metrics() (Codex MAJOR: health-only left a /metrics scrape stale until
    the first health poll)."""
    M_BRAKE_ENABLED.set(1 if UTILIZATION_TARGET > 0 else 0)


async def health(_request: web.Request) -> web.Response:
    """GET /__throttle/health — fast JSON snapshot of proxy + per-bearer state."""
    # Reflect status into the gauge for /metrics scrape; encoded as 1/0/-1.
    cs = state["central_status"]
    M_CENTRAL_STATUS.set({"up": 1, "down": 0}.get(cs, -1))
    # PR #573: zip limiter scheduler internals into the bearer view so
    # `curl /__throttle/health | jq '.bearers[].limiter.queued_per_client'`
    # surfaces starvation empirically without spelunking the process.
    bearers_view = {}
    for bid, bstate in bearer_state.items():
        view = dict(bstate)
        lim = bearer_limiters.get(bid)
        if lim is not None:
            view["limiter"] = lim.snapshot()
        bearers_view[bid] = view
    upstream_egress_ok, upstream_egress_error = await _check_upstream_egress()
    account_identity = None
    try:
        account_identity = _account_identity_verdict()
        _note_identity_collision(account_identity)
    except Exception as exc:
        # Health is load-bearing (invariant #4) — the identity guard must never
        # break it. Don't silently swallow (adversarial-review MEDIUM #2): a
        # recurring guard error should be visible, but health must still answer.
        log(f"account-identity guard error (non-fatal): {exc!r}")
        account_identity = None
    _publish_brake_enabled()
    body = {
        "inflight": state["inflight"],
        "queued": state["queued"],
        "served": state["served"],
        "client_disconnects": state["client_disconnects"],
        "upstream_retries": state["upstream_retries"],
        "max_concurrent": config.MAX_CONCURRENT,
        "queue_mode": config.QUEUE_MODE,
        "min_dispatch_gap_ms": int(config.MIN_DISPATCH_GAP_S * 1000),
        "upstream": config.UPSTREAM,
        "upstream_egress_ok": upstream_egress_ok,
        "upstream_egress_error": upstream_egress_error,
        "central_url": config.CENTRAL_URL,
        "central_status": cs,
        # FR-005: distinct-account guard — {collapsed,duplicates,suspected,
        # distinct,known} or null when unconfigured. duplicates non-empty ⇒ a
        # VERIFIED mutually-revoking same-account-in-two-stores collision (the
        # 09/07 outage). suspected non-empty ⇒ a shared email pending live-token
        # verification (a stale .claude.json label after a promote credential
        # swap looks identical until probed — the 10/07 false alarm).
        "account_identity": account_identity,
        # Pane-19 gap: surface whether the 7d/5h utilization brake is armed.
        # enabled=false means accounts can march to a hard 1.0 lockout unbraked.
        "brake": {
            "enabled": UTILIZATION_TARGET > 0,
            "target": UTILIZATION_TARGET,
            "warn": UTILIZATION_WARN,
        },
        "api_key": {
            "enabled": _api_key_candidate() is not None,
            "routing": config.API_KEY_ROUTING_MODE,
            "label": config.API_KEY_LABEL,
            "max_concurrent": config.API_KEY_MAX_CONCURRENT,
        },
        "central_last_check": state["central_last_check"],
        "last_advisor": state["last_advisor"],
        # PR #562/#573: per-bearer + per-client view so /__throttle/health
        # shows fleet parallelism + fair-RR queue depths in one glance.
        "bearers": bearers_view,
    }
    return web.json_response(body, status=200 if upstream_egress_ok else 503)


async def metrics(
    _request: web.Request,
) -> web.Response:
    """GET /metrics — Prometheus scrape endpoint."""
    M_INFLIGHT.set(state["inflight"])
    M_QUEUED.set(state["queued"])
    cs = state["central_status"]
    M_CENTRAL_STATUS.set({"up": 1, "down": 0}.get(cs, -1))
    _publish_brake_enabled()
    # aiohttp rejects charset in content_type kwarg → set full type via headers.
    return web.Response(
        body=generate_latest(REGISTRY),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


async def root_probe(
    request: web.Request,
) -> web.Response:
    """GET/HEAD / — local connectivity probe, never forwarded upstream."""
    if request.method == "HEAD":
        return web.Response(status=200)
    return web.Response(text="anthropic-throttle-proxy\n")


def _systemd_listen_sockets() -> list[socket.socket]:
    """Return socket-activation FDs passed by systemd, duplicated for aiohttp."""
    listen_fds_raw = os.environ.get("LISTEN_FDS")
    if not listen_fds_raw:
        return []
    try:
        listen_fds = int(listen_fds_raw)
    except ValueError:
        return []
    if listen_fds <= 0:
        return []

    listen_pid = os.environ.get("LISTEN_PID")
    if listen_pid:
        try:
            if int(listen_pid) != os.getpid():
                return []
        except ValueError:
            return []

    sockets: list[socket.socket] = []
    try:
        for fd in range(3, 3 + listen_fds):
            dup_fd = os.dup(fd)
            os.set_inheritable(dup_fd, False)
            sock = socket.socket(fileno=dup_fd)
            sock.setblocking(False)
            sockets.append(sock)
    except OSError:
        for sock in sockets:
            sock.close()
        raise
    finally:
        # Match sd_listen_fds(unset_environment=1): child processes should not
        # accidentally inherit stale activation metadata.
        for key in ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES"):
            os.environ.pop(key, None)
    return sockets


def main() -> None:
    """Boot the aiohttp app: bind locks, mount routes + UI, and serve forever."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # Record the process start time once. A step change in this gauge is a
    # restart — the only durable signal that the proxy was bounced mid-stream.
    M_START_TIME.set(time.time())
    # PR #22: re-apply any persisted /ui/config overrides on top of the env
    # defaults loaded at import time. Runs BEFORE limiter / pacing locks are
    # bound so any overridden MAX_CONCURRENT / AIMD_MIN / MIN_DISPATCH_GAP_S
    # is already in place when the first bearer-limiter is allocated.
    config.load_overrides()
    # PR #562 + #573: per-bearer FairBearerLimiter registry replaces the
    # single global Semaphore. Limiters are allocated lazily in
    # _get_bearer_limiter; the lock prevents racing dict.setdefault.
    _limiter.set_lock(asyncio.Lock())
    # Burst-smoothing lock (process-global, single source of pacing truth).
    _pacing.set_lock(asyncio.Lock())
    if config.CENTRAL_URL:
        loop.create_task(central_health_loop())
    app = web.Application(client_max_size=128 * 1024 * 1024)
    # Every response this proxy serves carries MARKER_HEADER so a downstream
    # local tier can tell a central-served 5xx (relay — keep central up) from
    # dokku nginx answering for a dead container (mark central down).
    app.on_response_prepare.append(stamp_proxy_marker)
    app.router.add_get("/", root_probe)
    app.router.add_get("/__throttle/health", health)
    app.router.add_get("/metrics", metrics)
    # UI + control plane (standalone-repo addition; mounted at /ui/*).
    from .ui.routes import attach_ui

    attach_ui(app)
    app.router.add_route("*", "/{path:.*}", handler)
    if config.log_mode:
        log(f"invalid THROTTLE_QUEUE_MODE={config.log_mode!r}; falling back to off")
    sockets = _systemd_listen_sockets()
    listen_desc = (
        f"{len(sockets)} systemd socket(s)"
        if sockets
        else f"{config.LISTEN_HOST}:{config.LISTEN_PORT}"
    )
    log(
        f"listening on {listen_desc} "
        f"max_concurrent={config.MAX_CONCURRENT} queue_mode={config.QUEUE_MODE} "
        f"upstream={config.UPSTREAM} central={config.CENTRAL_URL or '(direct)'} "
        f"dispatch_gap_ms={int(config.MIN_DISPATCH_GAP_S * 1000)}"
    )
    # shutdown_timeout: on SIGTERM, aiohttp closes the listener (no new conns)
    # then waits this long for in-flight streaming turns to finish before
    # force-closing. The bare default (config.SHUTDOWN_TIMEOUT_S=85s) sits under
    # systemd's 90s DefaultTimeoutStopSec so it is always honored; the NixOS
    # module couples a higher value with a matching TimeoutStopSec. Turns that
    # exceed the window are still cut.
    run_kwargs = {
        "print": None,
        "loop": loop,
        "shutdown_timeout": config.SHUTDOWN_TIMEOUT_S,
    }
    if sockets:
        run_kwargs["sock"] = sockets
    else:
        run_kwargs["host"] = config.LISTEN_HOST
        run_kwargs["port"] = config.LISTEN_PORT
    web.run_app(app, **run_kwargs)


if __name__ == "__main__":
    main()
