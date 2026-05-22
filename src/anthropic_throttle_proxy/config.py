"""Environment-derived configuration + process-global mutable state.

Everything the proxy reads from the environment lives here, along with the
shared ``state`` dict and the per-bearer registries that the Prometheus
collectors and the dashboard read. Importing this module has no side effects
beyond reading ``os.environ`` once at import time.

Tunables that are monkeypatched by the test-suite (``UTILIZATION_TARGET``,
``ADVISOR_ENABLED``, ``ADVISOR_DEBOUNCE_S``) are intentionally NOT here — they
live in :mod:`anthropic_throttle_proxy.proxy` so a ``setattr(proxy, ...)`` is
seen by the functions that read them. The AIMD tunables below are read as
plain constants (never patched) and so are safe to centralize.
"""

from __future__ import annotations

import os
import sys

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

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

state: dict[str, object] = {
    "inflight": 0,
    "queued": 0,
    "served": 0,
    "client_disconnects": 0,
    "upstream_retries": 0,
    "central_status": "unknown",
    "central_last_check": 0,
    # last_advisor holds {"text", "ts", "trigger"} from the GROQ advisor.
    "last_advisor": None,
}
# PR #562 + PR #573: bearer_id → FairBearerLimiter(MAX_CONCURRENT). Replaces
# the plain asyncio.Semaphore so two distinct OAuth bearers still get
# independent slot pools, AND within one bearer the slots are dispatched
# round-robin across distinct client TCPs so no claude-TUI starves.
# bearer_limiters maps bearer_id → FairBearerLimiter.
bearer_limiters: dict[str, object] = {}
# bearer_state maps bearer_id → {inflight, queued, served, clients}.
bearer_state: dict[str, dict[str, object]] = {}


def log(msg: str) -> None:
    """Write a single timestamp-free diagnostic line to stderr (unbuffered)."""
    sys.stderr.write(f"[anthropic-throttle] {msg}\n")
    sys.stderr.flush()
