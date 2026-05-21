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
"""

import asyncio
import collections
import hashlib
import json as _json
import os
import re
import sys
import time

import aiohttp
from aiohttp import web
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CollectorRegistry,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# Anthropic API pricing (USD per million tokens) — claude.com /pricing 2026-05.
# input | output | cache_read | cache_creation
# Used to compute estimated cost per request from the SSE usage block.
PRICING = {
    "claude-opus-4-7": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_creation": 1.25},
}
PRICING_DEFAULT = {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_creation": 18.75}


def _pricing_for(model):
    """Match a request model string to the pricing table, falling back to Opus rates."""
    if not model:
        return PRICING_DEFAULT
    for key, rates in PRICING.items():
        if model.startswith(key):
            return rates
    return PRICING_DEFAULT


# Prometheus metrics. Single CollectorRegistry so /metrics is fully isolated.
REGISTRY = CollectorRegistry()
M_REQUESTS = Counter(
    "anthropic_requests_total",
    "Requests processed by the throttle proxy, labeled by HTTP method, status, and Anthropic model.",
    ["method", "status", "model"],
    registry=REGISTRY,
)
M_TOKENS = Counter(
    "anthropic_tokens_total",
    "Tokens parsed from Anthropic SSE usage blocks.",
    ["model", "kind"],  # kind: input | output | cache_read | cache_creation
    registry=REGISTRY,
)
M_COST = Counter(
    "anthropic_cost_usd_total",
    "Estimated USD cost (from claude.com rate table) per token kind.",
    ["model", "kind"],
    registry=REGISTRY,
)
M_DURATION = Histogram(
    "anthropic_request_duration_seconds",
    "Wall-clock duration of forwarded requests (excluding queue wait).",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 60.0, 120.0, 300.0, 600.0),
    registry=REGISTRY,
)
M_INFLIGHT = Gauge("anthropic_inflight", "Current in-flight requests.", registry=REGISTRY)
M_QUEUED = Gauge("anthropic_queued", "Current queued requests waiting for the proxy limiter.", registry=REGISTRY)
# PR #562: per-bearer parallelism — distinct OAuth bearers run in distinct
# Semaphore slots so two Claude Max accounts on different hosts don't fight
# for one global slot. bearer_id = 8-hex-char sha256 of Authorization header.
M_INFLIGHT_BEARER = Gauge(
    "anthropic_inflight_per_bearer",
    "Current in-flight requests per bearer (8-hex sha256 of Authorization).",
    ["bearer"],
    registry=REGISTRY,
)
M_QUEUED_BEARER = Gauge(
    "anthropic_queued_per_bearer",
    "Current queued requests per bearer.",
    ["bearer"],
    registry=REGISTRY,
)
M_CLIENT_DISCONNECTS = Counter(
    "anthropic_client_disconnects_total",
    "Client disconnections during response stream (claude gave up / network blip).",
    registry=REGISTRY,
)
M_UPSTREAM_RETRIES = Counter(
    "anthropic_upstream_retries_total",
    "Upstream-error retries (via=central failover OR direct-upstream resilience).",
    registry=REGISTRY,
)
M_CENTRAL_STATUS = Gauge(
    "anthropic_central_status",
    "Central-tier health: 1=up, 0=down, -1=unknown (no central configured).",
    registry=REGISTRY,
)
# PR #575: AIMD ceiling per bearer + shrink counter.
M_AIMD_MAX = Gauge(
    "anthropic_aimd_max_concurrent",
    "Current AIMD ceiling (mutable per-bearer max_concurrent).",
    ["bearer"],
    registry=REGISTRY,
)
M_AIMD_SHRINKS = Counter(
    "anthropic_aimd_shrinks_total",
    "AIMD multiplicative-decrease events triggered by upstream rate pushback (429/503).",
    ["bearer", "status"],
    registry=REGISTRY,
)
M_AIMD_GROWS = Counter(
    "anthropic_aimd_grows_total",
    "AIMD additive-increase events after sustained successes past cooldown.",
    ["bearer"],
    registry=REGISTRY,
)
M_AIMD_OVERLOAD = Counter(
    "anthropic_overload_total",
    "Upstream 529 overloaded events (Anthropic-side capacity, not your usage). "
    "Does NOT shrink the ceiling; retry-after is still honored.",
    ["bearer"],
    registry=REGISTRY,
)
# Last-seen upstream rate-limit headroom per bearer (proactive-pacing signal).
M_RATELIMIT_REQUESTS_REMAINING = Gauge(
    "anthropic_ratelimit_requests_remaining",
    "Last-seen anthropic-ratelimit-requests-remaining header value.",
    ["bearer"],
    registry=REGISTRY,
)
M_RATELIMIT_TOKENS_REMAINING = Gauge(
    "anthropic_ratelimit_tokens_remaining",
    "Last-seen anthropic-ratelimit-tokens-remaining header value.",
    ["bearer"],
    registry=REGISTRY,
)
# OAuth (Claude Code Max/Pro) unified-window utilization (0..1). The OAuth
# regime is gated by a 5-hour rolling + 7-day weekly window rather than
# RPM/ITPM/OTPM, reported via anthropic-ratelimit-unified-*-utilization.
M_UTIL_5H = Gauge(
    "anthropic_ratelimit_unified_5h_utilization",
    "OAuth 5-hour rolling window utilization (0..1).",
    ["bearer"],
    registry=REGISTRY,
)
M_UTIL_7D = Gauge(
    "anthropic_ratelimit_unified_7d_utilization",
    "OAuth 7-day weekly window utilization (0..1).",
    ["bearer"],
    registry=REGISTRY,
)

UPSTREAM = os.environ.get("THROTTLE_UPSTREAM", "https://api.anthropic.com")
LISTEN_HOST = os.environ.get("THROTTLE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("THROTTLE_PORT", "8765"))
MAX_CONCURRENT = int(os.environ.get("CLAUDE_API_THROTTLE_MAX", "32"))
QUEUE_MODE = os.environ.get("THROTTLE_QUEUE_MODE", "off").strip().lower()
# PR #580: `observe` mode — no queue (request acquires a slot instantly,
# no fair-RR dispatch), but the AIMD shrink/grow counters DO move on
# upstream pushback. Result: clients see no slow-down, /__throttle/health
# + Prometheus show `max_concurrent` falling on 429 storms, and the
# operator gets the early-warning signal the wide-open `off` mode loses.
# `fair` and `reactive` keep queueing (current behaviour, both identical).
if QUEUE_MODE not in {"off", "observe", "fair", "reactive"}:
    log_mode = QUEUE_MODE
    QUEUE_MODE = "off"
else:
    log_mode = ""

# Burst pacing (standalone-repo #1, 20/05/2026): minimum gap in milliseconds
# between consecutive dispatches to the upstream / central. 0 = disabled.
# Smooths the ms-scale dogpile that hits Anthropic when 15 parallel TUIs all
# fire a request inside the same millisecond — orthogonal to MAX_CONCURRENT
# (which is the in-flight ceiling, not a rate cap). Recommended floor: 50
# (= 20 req/s peak burst to upstream); 100 = gentler 10 req/s peak.
MIN_DISPATCH_GAP_S = float(os.environ.get("THROTTLE_MIN_DISPATCH_GAP_MS", "0")) / 1000.0

# Central-tier opt-in: when set, the local proxy forwards each request to
# this URL instead of straight to upstream. Empty = direct upstream.
CENTRAL_URL = os.environ.get("THROTTLE_CENTRAL_URL", "").rstrip("/")
CENTRAL_HEALTH_PATH = "/__throttle/health"
CENTRAL_HEALTH_INTERVAL = float(os.environ.get("THROTTLE_CENTRAL_HEALTH_INTERVAL", "30"))
CENTRAL_HEALTH_TIMEOUT = float(os.environ.get("THROTTLE_CENTRAL_HEALTH_TIMEOUT", "5"))
CENTRAL_FORWARD_TIMEOUT = float(os.environ.get("THROTTLE_CENTRAL_FORWARD_TIMEOUT", "10"))

# PR #575: AIMD reactive throttle. "Wide open + throttle as we need":
# start at MAX_CONCURRENT (the hard ceiling), shrink multiplicatively on
# upstream pushback (429/503/529), and ramp back up additively after a
# cooldown of consecutive successes. Net: opens to the full hardware
# parallelism Pedro asks for, but backs off the moment Anthropic pushes
# back — no static cap to babysit.
AIMD_MIN = int(os.environ.get("THROTTLE_AIMD_MIN", "1"))
AIMD_BACKOFF_S = float(os.environ.get("THROTTLE_AIMD_BACKOFF_S", "30"))
AIMD_RAMP_AFTER = int(os.environ.get("THROTTLE_AIMD_RAMP_AFTER", "10"))
# AIMD multiplicative-decrease factor. TCP Reno halves (0.5, deep teeth, fast
# convergence, more wasted headroom); CUBIC cuts ~30% (0.7, shallower sawtooth,
# higher average utilisation). We default to 0.7 to glide closer to the limit
# after each pushback. Floor is AIMD_MIN.
AIMD_DECREASE = float(os.environ.get("THROTTLE_AIMD_DECREASE", "0.7"))
# Rate pushback → AIMD multiplicative-decrease (YOUR usage is too high).
AIMD_STATUSES = {429, 503}
# 529 = upstream OVERLOADED (Anthropic-side capacity, NOT your usage). We honor
# any retry-after and count it separately, but do NOT shrink the ceiling —
# shrinking would throttle you for someone else's capacity problem.
OVERLOAD_STATUSES = {529}
# Any throttle-ish status worth an advisor diagnosis.
THROTTLE_STATUSES = AIMD_STATUSES | OVERLOAD_STATUSES

# WS-B2: OAuth unified-window utilization pacing. When > 0, once the binding
# window's utilization crosses this fraction (while still "allowed"), shrink the
# ceiling one AIMD step to ease off BEFORE hitting "rejected" — "glide near the
# limit without hitting it". 0 = disabled (surface utilization only). The
# proactive pause on an already-"rejected" window is unconditional (it just
# preempts a 429 you'd otherwise get).
UTILIZATION_TARGET = float(os.environ.get("THROTTLE_UTILIZATION_TARGET", "0"))

# GROQ auto-advisor: on a throttle event, fire an out-of-band, debounced
# diagnosis to GROQ (an Anthropic-INDEPENDENT provider — see ui/advisor_impl.py,
# which calls GROQ's OpenAI-compatible endpoint over raw aiohttp). Off by
# default; needs ADVISOR_ENABLED=true + GROQ_API_KEY. Never on the hot path:
# scheduled as a fire-and-forget task whose failures are swallowed.
ADVISOR_ENABLED = os.environ.get("ADVISOR_ENABLED", "false").strip().lower() == "true"
ADVISOR_DEBOUNCE_S = float(os.environ.get("ADVISOR_DEBOUNCE_S", "120"))

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

state = {
    "inflight": 0,
    "queued": 0,
    "served": 0,
    "client_disconnects": 0,
    "upstream_retries": 0,
    "central_status": "unknown",
    "central_last_check": 0,
    "last_advisor": None,           # {"text", "ts", "trigger"} from the GROQ advisor
}
# PR #562 + PR #573: bearer_id → FairBearerLimiter(MAX_CONCURRENT). Replaces
# the plain asyncio.Semaphore so two distinct OAuth bearers still get
# independent slot pools, AND within one bearer the slots are dispatched
# round-robin across distinct client TCPs so no claude-TUI starves.
bearer_limiters = {}            # bearer_id → FairBearerLimiter
bearer_limiter_lock = None      # late-bound (needs running loop)
bearer_state = {}               # bearer_id → {"inflight", "queued", "served", "clients": {cid → {...}}}

# Burst pacing — process-global, single rate limiter across all bearers.
# Goal: never send two upstream POSTs closer than MIN_DISPATCH_GAP_S apart.
# Does NOT cap concurrency (MAX_CONCURRENT does that); only smooths the
# millisecond-scale dogpile. Lock acquired briefly; sleep is async.
_dispatch_lock = None           # late-bound (asyncio.Lock())
_last_dispatch_ts = 0.0


async def _pace_dispatch():
    """Block until at least MIN_DISPATCH_GAP_S has passed since the last
    upstream dispatch. No-op when MIN_DISPATCH_GAP_S <= 0."""
    global _last_dispatch_ts
    if MIN_DISPATCH_GAP_S <= 0:
        return
    async with _dispatch_lock:
        now = time.monotonic()
        wait = MIN_DISPATCH_GAP_S - (now - _last_dispatch_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_dispatch_ts = time.monotonic()


def log(msg):
    sys.stderr.write(f"[anthropic-throttle] {msg}\n")
    sys.stderr.flush()


def _bearer_id(headers):
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


def _client_id(request):
    """Identifies the originating client connection for fair-queueing.

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


class FairBearerLimiter:
    """Per-bearer concurrency limiter with weighted-fair-queueing across clients.

    Replaces a plain asyncio.Semaphore. Same in-flight cap (``max_concurrent``),
    but queued requests are picked round-robin across distinct ``client_id``s
    so no client can monopolize slots even under sustained backlog.

    Old: claude-A queues 50 tool calls just before claude-B queues 1 → B
    waits for ALL 50 of A's calls to drain (Semaphore FIFO acquire order).
    New: A and B interleave 1-for-1 — B's request goes through on the next
    free slot, not after A's entire backlog.
    """

    def __init__(self, max_concurrent, queue_mode):
        # PR #575: AIMD — `hard_max` is the operator-set ceiling (e.g. 32).
        # `max_concurrent` is the LIVE ceiling that AIMD adjusts down on
        # upstream 429/503/529 and slowly ramps back up after a cooldown.
        # Starting at hard_max = "wide open" by default; we only narrow if
        # Anthropic pushes back. Pedro's "wide open + throttle as we need".
        self.hard_max = max_concurrent
        self.max_concurrent = max_concurrent
        self.queue_mode = queue_mode
        # PR #580: split queue from observation. `observe` mode bypasses
        # the fair-RR queue (instant slot acquire) but DOES move AIMD
        # counters on 429/503/529 — gives /__throttle/health and the
        # Prometheus dashboard the early-warning signal that `off` loses
        # without re-introducing the queue-stall trade-off.
        self.queue_enabled = queue_mode in {"fair", "reactive"}
        self.observe_enabled = queue_mode != "off"
        self.inflight = 0
        self._queues = {}                          # client_id → deque[Future]
        self._rr_order = collections.deque()       # client_ids with pending requests
        self._lock = asyncio.Lock()
        self._last_throttle_at = 0.0               # monotonic-ish wall clock of last shrink
        self._successes_since_throttle = 0          # consecutive 2xx since last shrink
        self._retry_after_until = 0.0  # wall-clock end of any Retry-After window

    def slot(self, client_id):
        return _FairSlotContext(self, client_id)

    async def shrink(self):
        """AIMD multiplicative-decrease. Called on upstream rate pushback (429/503).

        Multiplies the live ceiling by AIMD_DECREASE (floor AIMD_MIN), records
        the throttle time, and resets the success counter. Always cuts by at
        least one slot so a fractional decrease can't stall at the same value.
        Already-inflight requests are NOT killed — they finish naturally, and
        `inflight` drops over time until it sinks below the new ceiling and
        `_try_dispatch` resumes.

        Returns the new ceiling.
        """
        # PR #580: `observe` mode shrinks counters (visible in
        # /__throttle/health + Prometheus) without affecting dispatch.
        # `off` skips entirely — no counter movement, no AIMD signal.
        if not self.observe_enabled:
            return None
        async with self._lock:
            scaled = int(self.max_concurrent * AIMD_DECREASE)
            new_max = max(AIMD_MIN, min(scaled, self.max_concurrent - 1))
            self.max_concurrent = new_max
            self._last_throttle_at = time.time()
            self._successes_since_throttle = 0
            return new_max

    async def grow(self):
        """AIMD additive-increase. Called after every successful 2xx response.

        Only ramps when ALL three guards hold:
          1. We've seen AIMD_RAMP_AFTER consecutive successes since the last
             shrink (don't react to a single lucky request after a 429 storm).
          2. AIMD_BACKOFF_S seconds have elapsed since the last shrink
             (give Anthropic's per-account counter room to drain).
          3. We are not already at hard_max (no point ramping past operator cap).

        Returns the new ceiling on bump, None otherwise. Always dispatches
        on bump so a queued request can grab the new slot immediately.
        """
        # PR #580: `observe` mode bumps counters without dispatching
        # (no queue exists). `off` skips entirely.
        if not self.observe_enabled:
            return None
        async with self._lock:
            self._successes_since_throttle += 1
            if self._successes_since_throttle < AIMD_RAMP_AFTER:
                return None
            if time.time() - self._last_throttle_at < AIMD_BACKOFF_S:
                return None
            # Don't ramp back up while the server's explicit Retry-After window
            # is still open, even if the AIMD cooldown already elapsed.
            if time.time() < self._retry_after_until:
                return None
            if self.max_concurrent >= self.hard_max:
                return None
            self.max_concurrent += 1
            self._successes_since_throttle = 0
            if self.queue_enabled:
                self._try_dispatch()
            return self.max_concurrent

    def note_retry_after(self, seconds):
        """Record an upstream Retry-After (seconds) for this bearer.

        The next dispatch waits at least this long (`wait_retry_after`), and
        `grow()` won't ramp back up until the window closes. Idempotent-ish:
        only extends the window, never shortens it. Honored uncapped — the
        Anthropic input bucket has been observed to return >120 s, so clamping
        would defeat the back-off.
        """
        if seconds <= 0:
            return self._retry_after_until
        until = time.time() + seconds
        if until > self._retry_after_until:
            self._retry_after_until = until
        self._last_throttle_at = max(self._last_throttle_at, time.time())
        return self._retry_after_until

    async def wait_retry_after(self):
        """Sleep until any outstanding Retry-After window has elapsed.

        Called just before dispatching to upstream so we honor the server's
        explicit back-off instead of spinning requests against a known-closed
        window. No-op when no Retry-After is pending.
        """
        wait = self._retry_after_until - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

    async def acquire(self, client_id):
        if not self.queue_enabled:
            async with self._lock:
                self.inflight += 1
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        async with self._lock:
            q = self._queues.setdefault(client_id, collections.deque())
            q.append(fut)
            if client_id not in self._rr_order:
                self._rr_order.append(client_id)
            self._try_dispatch()
        try:
            await fut
        except asyncio.CancelledError:
            # Caller gave up — remove queued future if still pending, or
            # release the slot if it was dispatched between set_result and
            # the cancellation reaching us.
            async with self._lock:
                q = self._queues.get(client_id)
                removed = False
                if q is not None:
                    try:
                        q.remove(fut)
                        removed = True
                    except ValueError:
                        pass
                    if not q:
                        self._queues.pop(client_id, None)
                        try:
                            self._rr_order.remove(client_id)
                        except ValueError:
                            pass
                if (
                    not removed
                    and fut.done()
                    and not fut.cancelled()
                    and fut.exception() is None
                ):
                    self.inflight -= 1
                    self._try_dispatch()
            raise

    async def release(self):
        async with self._lock:
            self.inflight -= 1
            self._try_dispatch()

    def _try_dispatch(self):
        """Caller must hold ``self._lock``."""
        while self.inflight < self.max_concurrent and self._rr_order:
            client_id = self._rr_order.popleft()
            q = self._queues.get(client_id)
            if not q:
                continue
            fut = q.popleft()
            if q:
                # Client has more queued — re-append at tail to keep rotation honest.
                self._rr_order.append(client_id)
            else:
                self._queues.pop(client_id, None)
            if fut.cancelled():
                continue
            self.inflight += 1
            fut.set_result(None)

    def snapshot(self):
        """Cheap dict snapshot for /__throttle/health."""
        return {
            "inflight": self.inflight,
            "max_concurrent": self.max_concurrent,
            "hard_max": self.hard_max,
            "queue_mode": self.queue_mode,
            "queue_enabled": self.queue_enabled,
            "observe_enabled": self.observe_enabled,
            "last_throttle_at": self._last_throttle_at,
            "successes_since_throttle": self._successes_since_throttle,
            "retry_after_until": self._retry_after_until,
            "queued_total": sum(len(q) for q in self._queues.values()),
            "queued_per_client": {cid: len(q) for cid, q in self._queues.items()},
            "rr_order": list(self._rr_order),
        }


class _FairSlotContext:
    """Async context manager returned by FairBearerLimiter.slot()."""

    def __init__(self, limiter, client_id):
        self.limiter = limiter
        self.client_id = client_id

    async def __aenter__(self):
        await self.limiter.acquire(self.client_id)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.limiter.release()
        return False


async def _get_bearer_limiter(bid):
    """Returns the FairBearerLimiter for a bearer, allocating on first sight."""
    lim = bearer_limiters.get(bid)
    if lim is not None:
        return lim
    async with bearer_limiter_lock:
        lim = bearer_limiters.get(bid)
        if lim is None:
            lim = FairBearerLimiter(MAX_CONCURRENT, QUEUE_MODE)
            bearer_limiters[bid] = lim
            bearer_state[bid] = {
                "inflight": 0, "queued": 0, "served": 0,
                "last_ratelimit": None,    # last-seen anthropic-ratelimit-* + retry-after
                "unified": None,           # parsed OAuth unified-window utilization
                "clients": {},
            }
            M_AIMD_MAX.labels(bearer=bid).set(MAX_CONCURRENT)
            log(f"bearer-new bid={bid} max_concurrent={MAX_CONCURRENT} hard_max={MAX_CONCURRENT} queue_mode={QUEUE_MODE}")
        return lim


_last_advice_ts = 0.0


def _advisor_snapshot(trigger_bid=None, trigger_status=None):
    """Assemble a JSON-safe view of proxy state for the advisor.

    Mirrors the dashboard's ``ui.routes._collect_view`` but adds the throttle
    trigger and avoids importing ui.routes, so it stays callable from the
    hot-path finally block without a circular import.
    """
    bearers = []
    for b, bs in bearer_state.items():
        lim = bearer_limiters.get(b)
        bearers.append({
            "bearer_id": b,
            "inflight": bs.get("inflight", 0),
            "queued": bs.get("queued", 0),
            "served": bs.get("served", 0),
            "last_ratelimit": bs.get("last_ratelimit"),
            "unified": bs.get("unified"),
            "limiter": lim.snapshot() if lim is not None else None,
        })
    return {
        "inflight": state["inflight"],
        "queued": state["queued"],
        "served": state["served"],
        "disconnects": state["client_disconnects"],
        "retries": state["upstream_retries"],
        "max_concurrent": MAX_CONCURRENT,
        "queue_mode": QUEUE_MODE,
        "min_dispatch_gap_ms": int(MIN_DISPATCH_GAP_S * 1000),
        "upstream": UPSTREAM,
        "central_url": CENTRAL_URL or "(direct)",
        "central_status": state["central_status"],
        "trigger": (
            {"bearer": trigger_bid, "status": trigger_status}
            if trigger_status is not None else None
        ),
        "bearers": bearers,
    }


async def _maybe_advise(trigger_bid, trigger_status):
    """Fire-and-forget GROQ diagnosis on a throttle event.

    Debounced to at most once per ADVISOR_DEBOUNCE_S so a 429 storm can't turn
    into a GROQ storm (GROQ's own free tier is ~30 RPM). Never raises — any
    failure is stored as the diagnosis text and logged. Runs in its own task,
    so the proxy hot path is unaffected regardless of outcome.
    """
    global _last_advice_ts
    if not ADVISOR_ENABLED or not os.environ.get("GROQ_API_KEY"):
        return
    now = time.time()
    if now - _last_advice_ts < ADVISOR_DEBOUNCE_S:
        return
    _last_advice_ts = now            # claim the window before awaiting (cheap de-dupe)
    trigger = f"status={trigger_status} bid={trigger_bid}"
    try:
        from .ui.advisor_impl import recommend
        text = await recommend(_advisor_snapshot(trigger_bid, trigger_status))
        state["last_advisor"] = {"text": text, "ts": now, "trigger": trigger}
        log(f"advisor {trigger}: {text[:160]!r}")
    except Exception as exc:
        state["last_advisor"] = {
            "text": f"(advisor error: {exc!s})", "ts": now, "trigger": trigger,
        }
        log(f"advisor-error {trigger}: {exc!r}")


async def central_health_loop():
    """Background poll of central /__throttle/health. Updates state."""
    if not CENTRAL_URL:
        return
    timeout = aiohttp.ClientTimeout(total=CENTRAL_HEALTH_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(CENTRAL_URL + CENTRAL_HEALTH_PATH) as r:
                    if r.status == 200:
                        await r.read()
                        if state["central_status"] != "up":
                            log(f"central {CENTRAL_URL} is UP")
                        state["central_status"] = "up"
                    else:
                        if state["central_status"] != "down":
                            log(f"central health returned {r.status} → DOWN")
                        state["central_status"] = "down"
            except Exception as exc:
                if state["central_status"] != "down":
                    log(f"central unreachable: {exc!r} → DOWN")
                state["central_status"] = "down"
            state["central_last_check"] = time.time()
            await asyncio.sleep(CENTRAL_HEALTH_INTERVAL)


def pick_target(path, query):
    """Choose upstream URL for this request: central if healthy, else direct."""
    if CENTRAL_URL and state["central_status"] == "up":
        base = CENTRAL_URL
        timeout = aiohttp.ClientTimeout(total=None, sock_read=600, sock_connect=CENTRAL_FORWARD_TIMEOUT)
        via = "central"
    else:
        base = UPSTREAM
        timeout = aiohttp.ClientTimeout(total=None, sock_read=600, sock_connect=30)
        via = "direct"
    url = f"{base}/{path}"
    if query:
        url += "?" + query
    return url, timeout, via


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


def _extract_ratelimit(headers):
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


def _parse_retry_after(meta):
    """Seconds from a Retry-After header (integer form). 0.0 if absent/unparseable.

    Anthropic uses integer-seconds Retry-After; the HTTP-date form is not
    emitted by the Messages API, so we don't parse it.
    """
    if not meta:
        return 0.0
    raw = meta.get("retry-after")
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def _publish_ratelimit_gauges(bid, meta):
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


def _parse_unified(meta):
    """Parse the OAuth unified-window headers into a compact dict.

    Returns {} when none are present (e.g. API-key traffic), which is itself a
    useful signal. utilization is a 0..1 float; reset values are epoch seconds.
    """
    if not meta or not any(k.startswith("anthropic-ratelimit-unified") for k in meta):
        return {}

    def _f(key):
        try:
            v = meta.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _i(key):
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


def _binding_utilization(unified):
    """The utilization of the window Anthropic flags as representative, with a
    max() fallback when the claim is missing/unknown."""
    u5h = unified.get("util_5h")
    u7d = unified.get("util_7d")
    claim = unified.get("representative_claim")
    if claim == "five_hour" and u5h is not None:
        return u5h
    if claim == "seven_day" and u7d is not None:
        return u7d
    candidates = [u for u in (u5h, u7d) if u is not None]
    return max(candidates) if candidates else None


async def _apply_unified(bid, bstate, limiter, meta):
    """React to OAuth unified-window headers (WS-B2).

    1. Surface utilization (gauges + bearer_state) — always.
    2. Proactive pause: if a window is already "rejected", stop dispatching to
       this bearer until its reset epoch — preempts the 429 + the
       ClientConnectionReset storm that comes with hammering an exhausted cap.
    3. Opt-in glide: when THROTTLE_UTILIZATION_TARGET > 0 and the binding window
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
        unified.get("status"), unified.get("status_5h"), unified.get("status_7d"),
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


def _extract_model_from_body(body):
    """Pull the `model` field from a POST /v1/messages JSON body."""
    if not body:
        return ""
    try:
        obj = _json.loads(body)
        return obj.get("model", "") or ""
    except Exception:
        # Last-resort regex if the body isn't quite JSON.
        m = re.search(rb'"model"\s*:\s*"([^"]+)"', body)
        return m.group(1).decode("utf-8", "ignore") if m else ""


# Match a 'data: {...}' SSE line carrying a `usage` block. Streamed responses
# emit message_start (with input usage) and message_delta (with output usage).
_USAGE_RE = re.compile(rb'"usage"\s*:\s*\{[^}]+\}')


def _parse_sse_usage(buf):
    """Extract aggregated usage counts from a buffered SSE response.
    Returns dict with input/output/cache_read/cache_creation token counts.
    Sums across message_start + message_delta usage blocks (Anthropic emits both).
    """
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for match in _USAGE_RE.finditer(buf):
        try:
            usage_obj = _json.loads(match.group(0).split(b":", 1)[1].lstrip())
        except Exception:
            continue
        # Anthropic field names → our shorter labels.
        totals["input"] += int(usage_obj.get("input_tokens") or 0)
        totals["output"] += int(usage_obj.get("output_tokens") or 0)
        totals["cache_read"] += int(usage_obj.get("cache_read_input_tokens") or 0)
        totals["cache_creation"] += int(usage_obj.get("cache_creation_input_tokens") or 0)
    return totals


async def _forward_once(request, headers, body, url, timeout):
    """Forward request to URL and stream the response back. Distinguishes
    client-side disconnects from upstream errors so the caller can retry
    upstream failures but not waste cycles retrying after the client gave up.
    Returns (response, status, captured_buffer, None, meta) on success
    or (None, None, None, exc, None) on upstream failure, where `meta` is the
    extracted upstream rate-limit headers (anthropic-ratelimit-* + retry-after).
    Raises ConnectionResetError / ClientConnectionResetError on client-side.
    """
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector, auto_decompress=False,
    ) as session:
        # Burst-smoothing pace (no-op when THROTTLE_MIN_DISPATCH_GAP_MS=0).
        # Placed inside the session context so the connector + TLS handshake
        # set-up time doesn't count against the gap budget — we pace the
        # actual upstream request issuance, not the prep.
        await _pace_dispatch()
        try:
            async with session.request(
                request.method, url,
                headers=headers, data=body, allow_redirects=False,
            ) as upstream:
                resp_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP_HEADERS
                }
                meta = _extract_ratelimit(upstream.headers)
                response = web.StreamResponse(status=upstream.status, headers=resp_headers)
                await response.prepare(request)
                # PR #557: tee the response into a small buffer so we can scan
                # for the SSE `usage` block AFTER the client finishes reading.
                # Cap at 1 MiB — usage block lives in the final few KB of any
                # claude response, even for long generations.
                captured = bytearray()
                cap_limit = 1024 * 1024
                async for chunk in upstream.content.iter_any():
                    if not chunk:
                        break
                    await response.write(chunk)
                    if len(captured) < cap_limit:
                        captured.extend(chunk[: cap_limit - len(captured)])
                await response.write_eof()
                return response, upstream.status, captured, None, meta
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return None, None, None, exc, None


async def handler(request):
    path = request.match_info.get("path", "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    body = await request.read() if request.body_exists else None

    # PR #557: extract model from POST /v1/messages body for metrics labels.
    model = _extract_model_from_body(body) if body else ""
    model_label = model or "unknown"

    # PR #562: choose limiter by bearer (two OAuth tokens → two slot pools).
    # PR #573: limiter is FairBearerLimiter, dispatched round-robin per client.
    bid = _bearer_id(request.headers)
    cid = _client_id(request)
    limiter = await _get_bearer_limiter(bid)
    bstate = bearer_state[bid]
    cstate = bstate["clients"].setdefault(cid, {"queued": 0, "inflight": 0, "served": 0})

    # PR #575: track whether the outer `queued` counter was incremented but
    # not yet decremented — if the caller is cancelled BETWEEN `state["queued"]
    # += 1` and the slot's body decrementing it, the finally block below
    # must roll back the counter or it leaks. Same for bstate/cstate.
    queued_incremented = False
    if limiter.queue_enabled:
        state["queued"] += 1
        bstate["queued"] += 1
        cstate["queued"] += 1
        queued_incremented = True
        M_QUEUED.set(state["queued"])
        M_QUEUED_BEARER.labels(bearer=bid).set(bstate["queued"])
        log(f"queue+ method={request.method} path=/{path} bid={bid} cid={cid} inflight={state['inflight']} queued={state['queued']}")
    else:
        log(f"queue-bypass method={request.method} path=/{path} bid={bid} cid={cid} inflight={state['inflight']} queue_mode={QUEUE_MODE}")

    try:
        async with limiter.slot(cid):
            if queued_incremented:
                state["queued"] -= 1
                bstate["queued"] -= 1
                cstate["queued"] -= 1
                queued_incremented = False
            state["inflight"] += 1
            bstate["inflight"] += 1
            cstate["inflight"] += 1
            M_QUEUED.set(state["queued"])
            M_QUEUED_BEARER.labels(bearer=bid).set(bstate["queued"])
            M_INFLIGHT.set(state["inflight"])
            M_INFLIGHT_BEARER.labels(bearer=bid).set(bstate["inflight"])
            url, timeout, via = pick_target(path, request.query_string)
            log(f"start  method={request.method} path=/{path} bid={bid} cid={cid} via={via} model={model_label} inflight={state['inflight']} queued={state['queued']}")
            # Honor any outstanding upstream Retry-After for this bearer before
            # we dispatch — don't spin a request against a known-closed window.
            await limiter.wait_retry_after()
            t0 = time.time()
            final_status = 0
            captured = None
            meta = None
            try:
                try:
                    response, status, captured, exc, meta = await _forward_once(request, headers, body, url, timeout)
                    if exc is None:
                        state["served"] += 1
                        final_status = status
                        return response
                except (ConnectionResetError, aiohttp.ClientConnectionResetError, asyncio.CancelledError) as cexc:
                    state["client_disconnects"] += 1
                    M_CLIENT_DISCONNECTS.inc()
                    log(f"client-disconnect path=/{path} {type(cexc).__name__}: {cexc} (no upstream retry)")
                    final_status = 499
                    return web.Response(status=499)

                if via == "central":
                    log(f"central forward failed: {exc!r} → marking DOWN, retrying direct")
                    state["central_status"] = "down"
                    state["upstream_retries"] += 1
                    M_UPSTREAM_RETRIES.inc()
                    url_direct, timeout_direct, _ = pick_target(path, request.query_string)
                    try:
                        response, status, captured, exc2, meta = await _forward_once(request, headers, body, url_direct, timeout_direct)
                        if exc2 is None:
                            state["served"] += 1
                            final_status = status
                            return response
                        exc = exc2
                    except (ConnectionResetError, aiohttp.ClientConnectionResetError, asyncio.CancelledError) as cexc:
                        state["client_disconnects"] += 1
                        M_CLIENT_DISCONNECTS.inc()
                        log(f"client-disconnect during direct-retry path=/{path}")
                        final_status = 499
                        return web.Response(status=499)
                else:
                    log(f"upstream-error path=/{path}: {exc!r} → retry direct once")
                    state["upstream_retries"] += 1
                    M_UPSTREAM_RETRIES.inc()
                    try:
                        response, status, captured, exc2, meta = await _forward_once(request, headers, body, url, timeout)
                        if exc2 is None:
                            state["served"] += 1
                            final_status = status
                            return response
                        exc = exc2
                    except (ConnectionResetError, aiohttp.ClientConnectionResetError, asyncio.CancelledError) as cexc:
                        state["client_disconnects"] += 1
                        M_CLIENT_DISCONNECTS.inc()
                        log(f"client-disconnect during upstream-retry path=/{path}")
                        final_status = 499
                        return web.Response(status=499)

                log(f"upstream-error final path=/{path}: {exc!r}")
                final_status = 502
                return web.Response(status=502, text=f"upstream error: {exc}\n")
            finally:
                state["inflight"] -= 1
                bstate["inflight"] -= 1
                cstate["inflight"] -= 1
                if final_status and 200 <= final_status < 400:
                    bstate["served"] += 1
                    cstate["served"] += 1
                M_INFLIGHT.set(state["inflight"])
                M_INFLIGHT_BEARER.labels(bearer=bid).set(bstate["inflight"])
                M_REQUESTS.labels(method=request.method, status=str(final_status), model=model_label).inc()
                M_DURATION.labels(model=model_label).observe(time.time() - t0)

                # Capture upstream rate-limit headroom for this bearer
                # (proactive-pacing signal + advisor input + dashboard view).
                if meta:
                    bstate["last_ratelimit"] = meta
                    _publish_ratelimit_gauges(bid, meta)
                    # WS-B2: OAuth unified-window utilization — surface it,
                    # preempt 429s on a rejected window, and (opt-in) glide.
                    try:
                        await _apply_unified(bid, bstate, limiter, meta)
                    except Exception as ue:
                        log(f"unified-error bid={bid}: {ue!r}")

                # AIMD + Retry-After feedback. final_status is whatever last
                # went to the client (central-retry-direct's status counts).
                try:
                    retry_after = _parse_retry_after(meta)
                    if final_status in AIMD_STATUSES:
                        # Rate pushback (429/503) → multiplicative-decrease so
                        # future requests queue here instead of being slammed
                        # against Anthropic's per-account counter.
                        new_max = await limiter.shrink()
                        M_AIMD_SHRINKS.labels(bearer=bid, status=str(final_status)).inc()
                        if new_max is not None:
                            M_AIMD_MAX.labels(bearer=bid).set(new_max)
                        if retry_after > 0:
                            limiter.note_retry_after(retry_after)
                        log(
                            f"aimd-shrink bid={bid} status={final_status} "
                            f"max_concurrent={new_max} retry_after={retry_after}"
                        )
                    elif final_status in OVERLOAD_STATUSES:
                        # 529 = upstream overloaded (not our usage): honor any
                        # retry-after but do NOT shrink the ceiling.
                        M_AIMD_OVERLOAD.labels(bearer=bid).inc()
                        if retry_after > 0:
                            limiter.note_retry_after(retry_after)
                        log(
                            f"overload bid={bid} status={final_status} "
                            f"retry_after={retry_after} (no shrink)"
                        )
                    elif final_status and 200 <= final_status < 400:
                        new_max = await limiter.grow()
                        if new_max is not None:
                            M_AIMD_GROWS.labels(bearer=bid).inc()
                            M_AIMD_MAX.labels(bearer=bid).set(new_max)
                            log(f"aimd-grow bid={bid} max_concurrent={new_max}")
                except Exception as aimde:
                    log(f"aimd-error bid={bid}: {aimde!r}")

                # Out-of-band GROQ advisor on any throttle signal
                # (fire-and-forget, debounced inside _maybe_advise). Fires even
                # in `off` mode. create_task so the hot path never awaits it.
                if ADVISOR_ENABLED and final_status in THROTTLE_STATUSES:
                    asyncio.create_task(_maybe_advise(bid, final_status))

                # PR #557: parse SSE usage block for token/cost metrics. Only do this
                # on successful POST /v1/messages — the only endpoint that streams a
                # usage block. Skip for HEAD/GET/health/etc.
                if captured and request.method == "POST" and "/v1/messages" in path:
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
                log(f"done   path=/{path} model={model_label} inflight={state['inflight']} queued={state['queued']} served={state['served']} disc={state['client_disconnects']} retries={state['upstream_retries']}")
    finally:
        # PR #575 B1 fix: if we got cancelled between incrementing `queued`
        # and the inner `async with limiter.slot()` body decrementing it,
        # roll back the queue counter so it doesn't leak forever and starve
        # the /__throttle/health gauge.
        if queued_incremented:
            state["queued"] -= 1
            bstate["queued"] -= 1
            cstate["queued"] -= 1
            M_QUEUED.set(state["queued"])
            M_QUEUED_BEARER.labels(bearer=bid).set(bstate["queued"])
            log(f"queue-leak-rollback bid={bid} cid={cid} (cancelled before slot dispatch)")


async def health(_request):
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
    return web.json_response({
        "inflight": state["inflight"],
        "queued": state["queued"],
        "served": state["served"],
        "client_disconnects": state["client_disconnects"],
        "upstream_retries": state["upstream_retries"],
        "max_concurrent": MAX_CONCURRENT,
        "queue_mode": QUEUE_MODE,
        "min_dispatch_gap_ms": int(MIN_DISPATCH_GAP_S * 1000),
        "upstream": UPSTREAM,
        "central_url": CENTRAL_URL,
        "central_status": cs,
        "central_last_check": state["central_last_check"],
        "last_advisor": state["last_advisor"],
        # PR #562/#573: per-bearer + per-client view so /__throttle/health
        # shows fleet parallelism + fair-RR queue depths in one glance.
        "bearers": bearers_view,
    })


async def metrics(_request):
    """Prometheus scrape endpoint."""
    M_INFLIGHT.set(state["inflight"])
    M_QUEUED.set(state["queued"])
    cs = state["central_status"]
    M_CENTRAL_STATUS.set({"up": 1, "down": 0}.get(cs, -1))
    # aiohttp rejects charset in content_type kwarg → set full type via headers.
    return web.Response(
        body=generate_latest(REGISTRY),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


def main():
    global bearer_limiter_lock
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # PR #562 + #573: per-bearer FairBearerLimiter registry replaces the
    # single global Semaphore. Limiters are allocated lazily in
    # _get_bearer_limiter; the lock prevents racing dict.setdefault.
    bearer_limiter_lock = asyncio.Lock()
    # Burst-smoothing lock (process-global, single source of pacing truth).
    global _dispatch_lock
    _dispatch_lock = asyncio.Lock()
    if CENTRAL_URL:
        loop.create_task(central_health_loop())
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app.router.add_get("/__throttle/health", health)
    app.router.add_get("/metrics", metrics)
    # UI + control plane (standalone-repo addition; mounted at /ui/*).
    from .ui.routes import attach_ui
    attach_ui(app)
    app.router.add_route("*", "/{path:.*}", handler)
    if log_mode:
        log(f"invalid THROTTLE_QUEUE_MODE={log_mode!r}; falling back to off")
    log(f"listening on {LISTEN_HOST}:{LISTEN_PORT} max_concurrent={MAX_CONCURRENT} queue_mode={QUEUE_MODE} upstream={UPSTREAM} central={CENTRAL_URL or '(direct)'} dispatch_gap_ms={int(MIN_DISPATCH_GAP_S * 1000)}")
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=None, loop=loop)


if __name__ == "__main__":
    main()
