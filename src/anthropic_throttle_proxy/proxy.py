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
import json
import os
import socket
import time
from typing import TYPE_CHECKING

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
from .forwarding import _forward_once, central_health_loop, pick_target
from .limiter import FairBearerLimiter, _get_bearer_limiter
from .metrics import (
    CONTENT_TYPE_LATEST,
    M_AIMD_GROWS,
    M_AIMD_MAX,
    M_AIMD_OVERLOAD,
    M_AIMD_SHRINKS,
    M_BODY_SHRINK_BYTES_SAVED,
    M_BODY_SHRINK_TRIMMED,
    M_CENTRAL_STATUS,
    M_CLIENT_DISCONNECTS,
    M_COST,
    M_DURATION,
    M_INFLIGHT,
    M_INFLIGHT_BEARER,
    M_QUEUED,
    M_QUEUED_BEARER,
    M_REQUESTS,
    M_START_TIME,
    M_TOKENS,
    M_UPSTREAM_RETRIES,
    M_UTIL_5H,
    M_UTIL_7D,
    REGISTRY,
    generate_latest,
)
from .pricing import _pricing_for
from .ratelimit import (
    _bearer_id,
    _binding_utilization,
    _client_id,
    _extract_model_from_body,
    _extract_ratelimit,
    _parse_retry_after,
    _parse_sse_usage,
    _parse_unified,
    _publish_ratelimit_gauges,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    "_client_id",
    "_extract_model_from_body",
    "_extract_ratelimit",
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
    # defined in this module
    "UTILIZATION_TARGET",
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


def _effective_admission(via: str) -> tuple[str, int]:
    """Admission mode and hard cap for this request.

    Local deployments normally run ``THROTTLE_QUEUE_MODE=off`` because the
    central tier owns the fleet-wide fair queue. Runtime evidence from Opus
    4.7/1M bursts showed central-only admission reacts too late for same-host
    dogpiles, so a central-backed local proxy still keeps a small fair queue.
    """
    if config.QUEUE_MODE == "off" and config.CENTRAL_URL:
        return "fair", max(1, min(config.CENTRAL_LOCAL_MAX_CONCURRENT, config.MAX_CONCURRENT))
    return config.QUEUE_MODE, config.MAX_CONCURRENT


def _effective_queue_mode(via: str) -> str:
    """Backward-compatible helper for tests and older callers."""
    return _effective_admission(via)[0]


def _get_direct_fallback_lock() -> asyncio.Lock:
    """Process-wide gate for direct retries after a central forward failure."""
    global _direct_fallback_lock
    if _direct_fallback_lock is None:
        _direct_fallback_lock = asyncio.Lock()
    return _direct_fallback_lock


async def _apply_unified(
    bid: str,
    bstate: dict[str, object],
    limiter: FairBearerLimiter,
    meta: Mapping[str, str],
) -> None:
    """React to OAuth unified-window headers (WS-B2).

    1. Surface utilization (gauges + bearer_state) — always.
    2. Proactive pause: if a window is already "rejected", stop dispatching to
       this bearer until its reset epoch — preempts the 429 + the
       ClientConnectionReset storm that comes with hammering an exhausted cap.
    3. Opt-in glide: when ``UTILIZATION_TARGET > 0`` and the binding window
       crosses it while still "allowed", shrink one AIMD step to ease off early.

    Never raises into the hot path (caller wraps in try/except).
    """
    unified = _parse_unified(meta)
    if not unified:
        return
    bstate["unified"] = unified
    if unified.get("util_5h") is not None:
        M_UTIL_5H.labels(bearer=bid).set(unified["util_5h"])
    if unified.get("util_7d") is not None:
        M_UTIL_7D.labels(bearer=bid).set(unified["util_7d"])

    # 2. Proactive pause when the server already says rejected.
    rejected = "rejected" in (
        unified.get("status"),
        unified.get("status_5h"),
        unified.get("status_7d"),
    )
    if rejected:
        reset = unified.get("reset") or unified.get("reset_5h") or unified.get("reset_7d") or 0
        pause = reset - time.time()
        if pause > 0:
            limiter.note_retry_after(pause)
            log(f"unified-rejected bid={bid} pause={int(pause)}s until reset (proactive 429-avoid)")
        return

    # 3. Opt-in proactive glide toward the cap.
    if UTILIZATION_TARGET > 0:
        binding = _binding_utilization(unified)
        if binding is not None and binding >= UTILIZATION_TARGET:
            new_max = await limiter.shrink()
            if new_max is not None:
                M_AIMD_SHRINKS.labels(bearer=bid, status="util").inc()
                M_AIMD_MAX.labels(bearer=bid).set(new_max)
                log(
                    f"util-shrink bid={bid} util={binding:.2f}>={UTILIZATION_TARGET} "
                    f"max_concurrent={new_max}"
                )


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


def _record_disconnect(
    path: str, where: str, exc: BaseException, attempt: _Attempt
) -> web.Response:
    """Account a client disconnect (no upstream retry) and build a 499 reply."""
    state["client_disconnects"] += 1
    M_CLIENT_DISCONNECTS.inc()
    if where == "first":
        log(f"client-disconnect path=/{path} {type(exc).__name__}: {exc} (no upstream retry)")
    else:
        log(f"client-disconnect {where} path=/{path}")
    attempt.final_status = 499
    return web.Response(status=499)


async def _try_forward(
    request: web.Request,
    headers: Mapping[str, str],
    body: bytes | None,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
    attempt: _Attempt,
) -> tuple[web.StreamResponse | None, Exception | None]:
    """One ``_forward_once`` call, recording success bookkeeping into ``attempt``.

    Returns ``(response, None)`` on success (and increments ``served``), or
    ``(None, exc)`` on an upstream error so the caller can decide whether to
    retry. Client-side disconnects propagate as exceptions to the caller.
    """
    response, status, captured, exc, meta = await _forward_once(
        request, headers, body, url, client_timeout
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
) -> web.StreamResponse | web.Response:
    """One direct retry after the first attempt's upstream error.

    A central failure marks central DOWN and retries against the direct
    upstream; a direct failure retries the same URL once. Returns the streamed
    response on success, a 499 on a client disconnect, or a 502 if the retry
    also fails.
    """
    if via == "central":
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
        return response
    log(f"upstream-error final path=/{path}: {exc2!r}")
    attempt.final_status = 502
    return web.Response(status=502, text=f"upstream error: {exc2}\n")


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
) -> web.StreamResponse | web.Response:
    """Forward once, then retry direct on upstream error. Behavior-identical to
    the original inline chain: a central failure marks central DOWN and retries
    direct; a direct failure retries direct once; client disconnects yield 499.
    """
    pushback_retries = 0
    while True:
        try:
            response, exc = await _try_forward(request, headers, body, url, client_timeout, attempt)
        except _CLIENT_DISCONNECT_EXC as cexc:
            return _record_disconnect(path, "first", cexc, attempt)
        if exc is not None:
            return await _retry_direct_once(
                request, headers, body, path, via, url, client_timeout, exc, attempt
            )
        if (
            response is not None
            and not response.prepared
            and attempt.final_status in THROTTLE_STATUSES
            and pushback_retries < config.RATE_PUSHBACK_RETRIES
        ):
            pushback_retries += 1
            await _aimd_feedback(bid, limiter, attempt)
            _schedule_advisor(bid, attempt.final_status)
            retry_after = _parse_retry_after(attempt.meta)
            pause = retry_after if retry_after > 0 else config.AIMD_BACKOFF_S
            log(
                f"rate-pushback-retry bid={bid} status={attempt.final_status} "
                f"retry={pushback_retries}/{config.RATE_PUSHBACK_RETRIES} "
                f"pause={pause} retry_after={retry_after}"
            )
            await limiter.wait_retry_after()
            continue
        return response


def _pushback_pause(meta: Mapping[str, str] | None) -> tuple[float, bool]:
    """Return the pause seconds and whether it was synthesized locally."""
    retry_after = _parse_retry_after(meta)
    if retry_after > 0:
        return retry_after, False
    return max(0.0, config.AIMD_BACKOFF_S), True


async def _aimd_feedback(bid: str, limiter: FairBearerLimiter, attempt: _Attempt) -> None:
    """Apply AIMD shrink/overload/grow + Retry-After feedback for one request."""
    final_status = attempt.final_status
    retry_after = _parse_retry_after(attempt.meta)
    if final_status in AIMD_STATUSES:
        # Rate pushback (429/503) → multiplicative-decrease so future requests
        # queue here instead of being slammed against Anthropic's counter.
        new_max = await limiter.shrink()
        M_AIMD_SHRINKS.labels(bearer=bid, status=str(final_status)).inc()
        if new_max is not None:
            M_AIMD_MAX.labels(bearer=bid).set(new_max)
        pause, synthetic_pause = _pushback_pause(attempt.meta)
        if pause > 0:
            limiter.note_retry_after(pause)
        log(
            f"aimd-shrink bid={bid} status={final_status} "
            f"max_concurrent={new_max} retry_after={retry_after} "
            f"pause={pause} synthetic_pause={synthetic_pause}"
        )
    elif final_status in OVERLOAD_STATUSES:
        # 529 = upstream overloaded (not our usage): honor any retry-after but
        # do NOT shrink the ceiling.
        M_AIMD_OVERLOAD.labels(bearer=bid).inc()
        pause, synthetic_pause = _pushback_pause(attempt.meta)
        if pause > 0:
            limiter.note_retry_after(pause)
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


async def handler(request: web.Request) -> web.StreamResponse:
    """Main reverse-proxy handler: queue, forward (with retry), and stream back.

    Acquires a per-bearer fair slot, picks central-or-direct upstream, forwards
    the request, streams the response, and on the way out applies AIMD feedback,
    publishes metrics, fires the optional advisor, and parses SSE usage.
    """
    path = request.match_info.get("path", "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    body = await request.read() if request.body_exists else None

    # PR #557: extract model from POST /v1/messages body for metrics labels.
    model = _extract_model_from_body(body) if body else ""
    model_label = model or "unknown"

    # PR #15: trim oversize POST /v1/messages bodies before forwarding so we
    # do not hand Anthropic a payload they will reject with the 32MB cap.
    # See body_shrink.py for the algorithm + trade-offs (cache invalidation,
    # breadcrumb stubs, hard floor on single-attachment overruns).
    if body is not None and request.method == "POST":
        body, shrink_meta = shrink_body(body, path)
        if shrink_meta.get("trimmed"):
            still = "true" if shrink_meta.get("still_oversize") else "false"
            M_BODY_SHRINK_TRIMMED.labels(model=model_label, still_oversize=still).inc()
            M_BODY_SHRINK_BYTES_SAVED.labels(model=model_label).inc(
                shrink_meta.get("bytes_saved", 0)
            )
            log(
                f"body_shrink bid={_bearer_id(request.headers)} model={model_label} "
                f"original={shrink_meta['original_bytes']} "
                f"final={shrink_meta['final_bytes']} "
                f"blocks_trimmed={shrink_meta['blocks_trimmed']} "
                f"saved={shrink_meta['bytes_saved']} "
                f"still_oversize={shrink_meta['still_oversize']}"
            )
            # When the proxy MUTATES the body we have to refresh Content-Length;
            # the header dict we forward was built from the ORIGINAL request and
            # would lie about the payload size if we left it untouched.
            headers["Content-Length"] = str(len(body))
        elif "v1/messages" in path and shrink_meta.get("original_bytes") is not None:
            # PR #17: passthrough diagnostic. body_shrink only logs when it
            # actually trims; the no-trim case is silent. That made it
            # impossible to correlate Anthropic 413s with the actual request
            # size — the operator could only see `served=413` in the done
            # line and had to guess whether the body was 5 MiB or 31 MiB.
            # This terse log fires for every POST /v1/messages that
            # body_shrink did not rewrite, so the bytes-on-the-wire are
            # always observable. ``reason`` is "under-cap" when the body
            # was already small enough (the common case) and otherwise
            # carries the bail-out reason from body_shrink (``non-json``,
            # ``no-messages-array``, ``disabled``…). Counter and metric
            # remain unchanged — this is purely a log signal.
            reason = shrink_meta.get("reason", "under-cap")
            log(
                f"body_passthrough bid={_bearer_id(request.headers)} "
                f"model={model_label} bytes={shrink_meta['original_bytes']} "
                f"reason={reason}"
            )

    bid = _bearer_id(request.headers)
    cid = _client_id(request)
    url, client_timeout, via = pick_target(path, request.query_string)
    queue_mode, hard_max = _effective_admission(via)
    # PR #562 chooses the limiter by bearer, so two OAuth tokens get two
    # independent slot pools. PR #573 makes that limiter a FairBearerLimiter,
    # dispatched round-robin per client connection.
    limiter = await _get_bearer_limiter(bid, queue_mode, hard_max)
    bstate = bearer_state[bid]
    cstate = bstate["clients"].setdefault(cid, {"queued": 0, "inflight": 0, "served": 0})
    counters = _Counters(bid, cid, bstate, cstate)

    if limiter.queue_enabled:
        counters.enqueue(request, path)
    else:
        log(
            f"queue-bypass method={request.method} path=/{path} bid={bid} "
            f"cid={cid} inflight={state['inflight']} queue_mode={config.QUEUE_MODE}"
        )

    try:
        async with limiter.slot(cid):
            counters.dequeue()
            counters.enter_inflight()
            log(
                f"start  method={request.method} path=/{path} bid={bid} cid={cid} "
                f"via={via} model={model_label} inflight={state['inflight']} "
                f"queued={state['queued']}"
            )
            # Honor any outstanding upstream Retry-After for this bearer before
            # we dispatch — don't spin a request against a known-closed window.
            await limiter.wait_retry_after()
            t0 = time.time()
            attempt = _Attempt()
            try:
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
    finally:
        # PR #575 B1 fix: if we got cancelled between incrementing `queued`
        # and the inner `async with limiter.slot()` body decrementing it,
        # roll back the queue counter so it doesn't leak forever and starve
        # the /__throttle/health gauge.
        if counters.queued_incremented:
            counters.dequeue()
            log(f"queue-leak-rollback bid={bid} cid={cid} (cancelled before slot dispatch)")


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
    return web.json_response(
        {
            "inflight": state["inflight"],
            "queued": state["queued"],
            "served": state["served"],
            "client_disconnects": state["client_disconnects"],
            "upstream_retries": state["upstream_retries"],
            "max_concurrent": config.MAX_CONCURRENT,
            "queue_mode": config.QUEUE_MODE,
            "min_dispatch_gap_ms": int(config.MIN_DISPATCH_GAP_S * 1000),
            "upstream": config.UPSTREAM,
            "central_url": config.CENTRAL_URL,
            "central_status": cs,
            "central_last_check": state["central_last_check"],
            "last_advisor": state["last_advisor"],
            # PR #562/#573: per-bearer + per-client view so /__throttle/health
            # shows fleet parallelism + fair-RR queue depths in one glance.
            "bearers": bearers_view,
        }
    )


async def metrics(_request: web.Request) -> web.Response:
    """GET /metrics — Prometheus scrape endpoint."""
    M_INFLIGHT.set(state["inflight"])
    M_QUEUED.set(state["queued"])
    cs = state["central_status"]
    M_CENTRAL_STATUS.set({"up": 1, "down": 0}.get(cs, -1))
    # aiohttp rejects charset in content_type kwarg → set full type via headers.
    return web.Response(
        body=generate_latest(REGISTRY),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


async def root_probe(request: web.Request) -> web.Response:
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
