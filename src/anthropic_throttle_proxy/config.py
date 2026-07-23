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
MAX_CONCURRENT = int(os.environ.get("CLAUDE_API_THROTTLE_MAX", "3"))
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

# Graceful-shutdown drain window. aiohttp's web.run_app stops accepting new
# connections on SIGTERM, then waits this long for in-flight requests (streaming
# /v1/messages turns) to finish before force-closing them. aiohttp's default is
# 60s; a deploy/restart with turns in flight force-closes them (the 29/05/2026
# fleet-wide "socket connection closed unexpectedly").
#
# The bare default here is 85s — deliberately UNDER systemd's 90s
# DefaultTimeoutStopSec — so aiohttp finishes its graceful drain+close before
# systemd would SIGKILL, i.e. this value is honored even under a stock unit.
# Raising it past ~90s only takes effect if the supervising unit's
# TimeoutStopSec is raised to match; the NixOS module sets BOTH this env var and
# TimeoutStopSec from one coupled option so they cannot drift. Turns that do not
# finish within the window are still cut — pair with not restarting under load.
SHUTDOWN_TIMEOUT_S = float(os.environ.get("THROTTLE_SHUTDOWN_TIMEOUT_S", "85"))

# Burst pacing (standalone-repo #1, 20/05/2026): minimum gap in milliseconds
# between consecutive dispatches to the upstream / central. 0 = disabled.
# Smooths the ms-scale dogpile that hits Anthropic when 15 parallel TUIs all
# fire a request inside the same millisecond — orthogonal to MAX_CONCURRENT
# (which is the in-flight ceiling, not a rate cap). Recommended floor: 50
# (= 20 req/s peak burst to upstream); 100 = gentler 10 req/s peak.
MIN_DISPATCH_GAP_S = float(os.environ.get("THROTTLE_MIN_DISPATCH_GAP_MS", "0")) / 1000.0

# Priority lane (03/07/2026 fix): short/latency-sensitive calls — the /goal
# Stop-hook evaluator (small max_tokens, no tools) — dispatch through a
# DEDICATED pool of PRIORITY_RESERVE_SLOTS, independent of the main AIMD pool,
# so they never starve behind long generations holding every main slot
# (verified: a 24s evaluator waited 46s in the FIFO past its 30s client
# timeout → disconnected → Claude Code shows the misleading "model (sonnet)"
# error → /goal halts). The pool is a deliberate, bounded overshoot: total
# upstream concurrency ≤ max_concurrent + reserve. Priority calls still honour
# Retry-After before dispatch. A request is "short" iff it parses as
# 0 < max_tokens <= PRIORITY_MAX_TOKENS AND carries no tools AND its body is
# ≤ PRIORITY_MAX_BODY_BYTES (so a giant no-tools generation that happens to
# set a small max_tokens cannot jump the queue). Reserve 0 disables the lane
# (priority calls demote to normal round-robin traffic).
PRIORITY_RESERVE_SLOTS = max(0, int(os.environ.get("THROTTLE_PRIORITY_RESERVE_SLOTS", "0")))
PRIORITY_MAX_TOKENS = max(0, int(os.environ.get("THROTTLE_PRIORITY_MAX_TOKENS", "8192")))
PRIORITY_MAX_BODY_BYTES = max(0, int(os.environ.get("THROTTLE_PRIORITY_MAX_BODY_BYTES", "262144")))

# Central-tier opt-in: when set, the local proxy forwards each request to
# this URL instead of straight to upstream. Empty = direct upstream.
CENTRAL_URL = os.environ.get("THROTTLE_CENTRAL_URL", "").rstrip("/")
# Local safety valve for desktop/client-side proxies that normally run
# THROTTLE_QUEUE_MODE=off because a central tier is configured. Central still
# owns fleet-wide admission, but the local tier must not pass a same-host burst
# through unbounded: Claude Code can launch several large Opus requests before
# central/AIMD feedback arrives. This cap is only used by that central-backed
# local safety mode; explicit fair/reactive deployments keep MAX_CONCURRENT.
CENTRAL_LOCAL_MAX_CONCURRENT = int(os.environ.get("THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT", "2"))
CENTRAL_HEALTH_PATH = "/__throttle/health"
CENTRAL_HEALTH_INTERVAL = float(os.environ.get("THROTTLE_CENTRAL_HEALTH_INTERVAL", "30"))
CENTRAL_HEALTH_TIMEOUT = float(os.environ.get("THROTTLE_CENTRAL_HEALTH_TIMEOUT", "5"))
CENTRAL_FORWARD_TIMEOUT = float(os.environ.get("THROTTLE_CENTRAL_FORWARD_TIMEOUT", "10"))
UPSTREAM_HEALTH_TIMEOUT = float(os.environ.get("THROTTLE_UPSTREAM_HEALTH_TIMEOUT", "10"))
# Per-read socket timeout on the FORWARD path (inter-byte, not total). A stalled
# upstream that emits no bytes for this many seconds raises SocketTimeoutError,
# which the proxy turns into a single retry-direct. Default 600 preserves the
# historic behavior; for upstreams that can stall silently (e.g. z.ai mid long
# max_tokens generation), set this BELOW the client idle timeout (~60 s) so the
# retry fires while the client is still connected — otherwise the late write
# hits a closing transport and the truncated HTTP surfaces client-side as
# InvalidHTTPResponse (23/07 :8766 incident: a glm-5.2 POST hung 621 s against a
# 600 s sock_read). The Anthropic lane streams SSE keepalives during reasoning,
# which reset sock_read, so it is tolerant of a lower value too; keep the
# default conservative and tune per-instance via THROTTLE_UPSTREAM_SOCK_READ_TIMEOUT_S.
UPSTREAM_SOCK_READ_TIMEOUT = float(os.environ.get("THROTTLE_UPSTREAM_SOCK_READ_TIMEOUT_S", "600"))
UPSTREAM_HEALTH_INTERVAL = float(os.environ.get("THROTTLE_UPSTREAM_HEALTH_INTERVAL", "30"))
# Central-health hysteresis: a single transient probe miss must NOT abandon
# central — that flips the whole local fleet to direct fallback and risks an
# unqueued firehose (the 25/05/2026 incident shape). Require FAIL_THRESHOLD
# consecutive failed probes before declaring central DOWN, and OK_THRESHOLD
# consecutive healthy probes before re-declaring UP. Asymmetric on purpose: slow
# to drop (protects the data plane), modest to re-adopt (don't trust a flapping
# central immediately). Both floor at 1 = legacy flip-on-every-sample behaviour.
CENTRAL_HEALTH_FAIL_THRESHOLD = max(
    1, int(os.environ.get("THROTTLE_CENTRAL_HEALTH_FAIL_THRESHOLD", "3"))
)
CENTRAL_HEALTH_OK_THRESHOLD = max(
    1, int(os.environ.get("THROTTLE_CENTRAL_HEALTH_OK_THRESHOLD", "2"))
)

# PR #575: AIMD reactive throttle, revised by PR #40 into cap discovery:
# `MAX_CONCURRENT` is only the hard upper bound; new bearers start at
# `AIMD_INITIAL_CONCURRENT`, grow additively after clean successes, and shrink
# multiplicatively on upstream pushback. Net: the proxy discovers the account's
# current usable parallelism instead of requiring a static cap to babysit.
AIMD_MIN = max(1, int(os.environ.get("THROTTLE_AIMD_MIN", "1")))
AIMD_INITIAL_CONCURRENT = max(
    AIMD_MIN, int(os.environ.get("THROTTLE_AIMD_INITIAL_CONCURRENT", str(AIMD_MIN)))
)
AIMD_BACKOFF_S = float(os.environ.get("THROTTLE_AIMD_BACKOFF_S", "30"))
# Short cooldown for a 429/503 that is CONCURRENCY/rate pushback, not budget
# exhaustion. Anthropic returns 429-with-no-Retry-After for BOTH: a 5h/7d
# budget soft-throttle AND a per-account concurrency/requests-per-minute cap.
# Only the first warrants the full AIMD_BACKOFF_S pause (hold until the window
# eases); a concurrency 429 clears the instant inflight drops below the cap, so
# AIMD shrink alone sheds the load and a full 30s pause just collapses the
# active account to cap=1 and holds it there under fleet load (03/07/2026
# single-active concentration incident: 52 tabs → one account → concurrency
# 429s misread as budget → sustained stall while Anthropic reported
# unified-status=allowed at 19%/23%). The two are told apart by the unified
# budget headers on the SAME response (see `_pushback_pause`). Hot-tunable.
CONCURRENCY_COOLDOWN_S = max(0.0, float(os.environ.get("THROTTLE_CONCURRENCY_COOLDOWN_S", "2")))
AIMD_RAMP_AFTER = int(os.environ.get("THROTTLE_AIMD_RAMP_AFTER", "10"))
# Adaptive ramp (PR #53, 06/06/2026 stall incident): the live cap recovers via
# additive-increase every ``AIMD_RAMP_AFTER`` consecutive 200s, but a fixed
# threshold cannot tell an *isolated* transient 429 from a *sustained* storm —
# both pay the same recovery cost. RAMP_AFTER=10 × per-req ≈11s ≈ ~110s tail
# after every shrink (this dominates the AIMD_BACKOFF_S=30 cooldown), including
# ones that should have been single blips. Adaptive ramp keeps the slow path
# for storms (≥STORM_THRESHOLD shrinks inside the 2× AIMD_BACKOFF_S lookback
# window — likely sustained pushback, don't ramp fast)
# and switches to RAMP_AFTER_FAST when the shrink history is sparse (likely
# transient, recover quickly). The BACKOFF_S cooldown still gates ramps inside
# every active storm, so faster ramping cannot oscillate the ceiling under load.
AIMD_RAMP_AFTER_FAST = max(1, int(os.environ.get("THROTTLE_AIMD_RAMP_AFTER_FAST", "5")))
# Upper bound on STORM_THRESHOLD. The limiter's `_shrink_history` deque is sized
# to exactly this many timestamps, so `_recent_shrinks` can count up to (and
# storm mode `recent >= threshold` is reachable for) any value in [1, MAX]. A
# threshold above the deque depth could never be counted (recent caps at the
# deque length), silently forcing FAST during real storms — the bug Codex caught
# on #53. Keep this == the deque maxlen == the aimd_storm_threshold knob's max.
AIMD_STORM_THRESHOLD_MAX = 100
AIMD_STORM_THRESHOLD = max(
    1, min(AIMD_STORM_THRESHOLD_MAX, int(os.environ.get("THROTTLE_AIMD_STORM_THRESHOLD", "3")))
)
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
# SSE keepalive-hold (spec 092): for a STREAMING POST /v1/messages that hits a
# TRANSIENT throttle (529, central-queue-depth 503, or concurrency 429/503),
# commit a "200 text/event-stream" client response immediately and emit SSE ":"
# keepalive comment frames at this interval while internally retrying.  The
# comments pass through to the client but are silently DROPPED by the Anthropic
# SDK parser (spec 092 invariant 3), so the client sees a slow response rather
# than a banner. On a budget-rejected / long Retry-After / non-streaming request,
# the existing clean-error path is used unchanged. The hold consumes the SAME
# end-to-end wait-budget as the fair-queue timeout, so local + central wait times
# cannot stack past client patience (spec 092 invariant 6). budget 0 -> no hold.
# True = hold enabled; False = old pass-through behavior (hot-tunable).
KEEPALIVE_HOLD = os.environ.get("THROTTLE_KEEPALIVE_HOLD", "true").strip().lower() != "false"
# Interval between keepalive comment frames (milliseconds). Must be well under
# the client idle-timeout (~60 s) to prevent the 07/07 truncated-HTTP footgun.
# Default 10 s provides 6 heartbeats before even the most aggressive 60 s abort.
KEEPALIVE_INTERVAL_MS = max(500, int(os.environ.get("THROTTLE_KEEPALIVE_INTERVAL_MS", "10000")))
# When upstream returns HTTP pushback before streaming a response body, hold the
# client request and retry after the AIMD/Retry-After pause instead of handing the
# first transient 429/503/529 directly to Claude.
RATE_PUSHBACK_RETRIES = max(0, int(os.environ.get("THROTTLE_RATE_PUSHBACK_RETRIES", "1")))
# Maximum Retry-After window we will keep an HTTP request open for. Anthropic can
# return short-lived 30s throttles for temporary capacity/acceleration pushback;
# holding those hides a transient from Claude Code. Account-window Retry-After
# values can be measured in hours, though; honoring those inside a live client
# request just creates local gateway timeouts and a queue pile-up. Longer windows
# are still recorded on the bearer limiter, but current and new requests fail
# fast with 429 instead of sleeping behind the proxy.
MAX_HOLD_RETRY_AFTER_S = max(0.0, float(os.environ.get("THROTTLE_MAX_HOLD_RETRY_AFTER_S", "60")))

# Upper bound on time a request may WAIT IN THE FAIR QUEUE for a slot before
# the proxy fails fast with a clean 503 + Retry-After. Claude Code shows
# "Waiting for API response" after ~20 s of silence and aborts the socket at
# ~60 s; any response written after that hits a closing transport, reaches the
# client as truncated HTTP, and Node's fetch surfaces it as
# `InvalidHTTPResponse` — which claude-code then misreads as a 401 login
# failure (07/07/2026 fleet incident: 2/5 bearers usable → 60-80 s queue
# waits → phantom-401 storm). Failing fast INSIDE the client's patience window
# keeps the transport alive so the SDK retries transparently ("API Error:
# 503 · Retrying…") and re-enters the round-robin fairly. 0 disables the bound
# (historical unbounded wait). Orthogonal to MAX_HOLD_RETRY_AFTER_S above,
# which gates only upstream Retry-After holds, not queue time.
QUEUE_MAX_WAIT_S = max(0.0, float(os.environ.get("THROTTLE_QUEUE_MAX_WAIT_S", "30")))
# Retry-After attached to the queue-wait-timeout 503. Short on purpose: the
# retry re-enters the per-client round-robin fairly, and the SDK layers its
# own exponential backoff on repeated failures.
QUEUE_TIMEOUT_RETRY_AFTER_S = 5

# Optional JSON file for per-bearer Retry-After windows. Live hotfix deploys
# restart the user service while account windows are still cooling down; without
# a persisted floor, the new process forgets the window and rediscovers it with
# one fresh 429. Empty default keeps tests and stateless containers side-effect
# free. Nix/host units should set this to an XDG state path.
RETRY_AFTER_STATE_FILE = os.environ.get("THROTTLE_RETRY_AFTER_STATE_FILE", "").strip()

# Ceiling on a Retry-After window RESTORED from the state file (seconds; 0 =
# uncapped legacy). A live-noted window is evidence; a restored one is hearsay
# from a previous process. Budget windows noted at 100% exhaustion end at the
# account's reset epoch, but the rolling 5h/7d usage decays underneath — the
# 13-14/07 incident kept accounts back at 91-92% blocked for ~58 h because
# every restart resurrected the full window (and the #101 poller gate then
# silenced the usage evidence that would have contradicted it). A capped
# restore self-corrects: if the account is still exhausted, the first request
# after the cap re-notes the honest window from a live 429.
RETRY_AFTER_RESTORE_CAP_S = max(
    0.0, float(os.environ.get("THROTTLE_RETRY_AFTER_RESTORE_CAP_S", "900"))
)

# Ceiling on a LIVE "rejected"-window pause noted from `_maybe_pause_rejected`
# (seconds; 0 = uncapped legacy "pause until reset"). A rejected budget window
# ends at the account's reset epoch — potentially days out. The central tier
# runs no usage poller (no cred paths), so without a cap a live-noted window
# whose status eases back to allowed_warning blocks the bearer for the full
# window until a restart/healthcheck (the 13-14/07 shape on the central).
# Capping bounds it to a re-probe cadence: if still rejected, the next request
# re-notes from a live 429; account routing + AIMD contain the probe cost.
RETRY_AFTER_REJECT_CAP_S = max(
    0.0, float(os.environ.get("THROTTLE_RETRY_AFTER_REJECT_CAP_S", "900"))
)

# Z.ai Coding Plan sends quota-window resets in the JSON error body, not a
# Retry-After header. Add a small jitter so a fleet sharing one key does not all
# resume on the same reset second.
ZAI_QUOTA_RESET_JITTER_S = max(
    0.0, float(os.environ.get("THROTTLE_ZAI_QUOTA_RESET_JITTER_S", "15"))
)

# Storm early-warning: when the process-global upstream-retry counter crosses
# this threshold, the proxy emits ONE WARNING line (likely a stale-token / 429
# storm). It does not change throttle behaviour — purely an observability hint
# so a retry pile-up is greppable instead of buried in per-request "done" lines.
STORM_WARN_RETRIES = int(os.environ.get("THROTTLE_STORM_WARN_RETRIES", "25"))

# Optional local credential map: "LABEL:/path/to/.credentials.json,LABEL:..."
# maps Claude Code credential files to account labels so /ui can name bearers
# and show per-account 5h/7d usage. When THROTTLE_ACCOUNT_ROUTING is
# least_loaded or budget_paced, the same map is also used by the hot-path router
# below. Unset (the default, and always on the central Dokku tier where no cred
# files exist) hides the panel entirely and no file is read.
ACCOUNT_CRED_PATHS = os.environ.get("THROTTLE_ACCOUNT_CRED_PATHS", "")

# THROTTLE_ACTIVE_CRED_PATH names the single credential file the whole fleet
# reads (e.g. ~/.claude/.credentials.json) under the single-active-account
# failover model. A captive broker swaps that file between accounts on a 7d
# limit; the hot path compares each EXHAUSTED request's bearer against this
# file's current bearer and, when they differ, returns a local 401 "nudge" so
# the stale tab re-reads the swapped credential (claude's 401 self-heal) and
# adopts the live account, instead of being fast-failed for the multi-day
# Retry-After. Unset (the default, and always on the central Dokku tier) keeps
# the unchanged fast-fail behavior. The token is read, hashed to its 8-hex
# bearer_id, and dropped — never logged (invariant #2).
ACTIVE_CRED_PATH = os.environ.get("THROTTLE_ACTIVE_CRED_PATH", "").strip()

# Opt-in hot-path account routing. When enabled on a local proxy with
# THROTTLE_ACCOUNT_CRED_PATHS configured, the proxy selects the best usable
# credential file for each /v1/messages request and rewrites only the upstream
# Authorization header. This is the only way already-running Claude sessions can
# actually share multiple accounts; otherwise every long-lived process pins the
# bearer it read at startup. Defaults off because central Dokku tiers usually do
# not have local credential files.
#
# Modes: "least_loaded" ranks on raw utilization (queue/inflight first);
# "budget_paced" (spec 4) ranks on deadline-aware pacing — each account's 5h/7d/
# scoped window is treated as a resource budget scored by how fast it burns
# relative to its reset, so windows near a reset with slack are cheaper and a
# hot early-cycle window is expensive. Same hard safety gates in both modes.
_ACCOUNT_ROUTING_MODE = os.environ.get("THROTTLE_ACCOUNT_ROUTING", "off").strip().lower()
ACCOUNT_ROUTING_MODE = (
    _ACCOUNT_ROUTING_MODE
    if _ACCOUNT_ROUTING_MODE in {"off", "least_loaded", "budget_paced"}
    else "off"
)

# Optional metered overflow bearer. This deliberately reads the secret from a
# runtime file instead of putting the key in the service environment or Nix
# store. ``prefer`` sends /v1/messages to the API key first (OAuth accounts stay
# fallback); ``overflow`` uses it only after the configured OAuth router cannot
# find a usable account. Empty file path or "off" keeps the feature disabled.
API_KEY_FILE = os.environ.get("THROTTLE_API_KEY_FILE", "").strip()
_API_KEY_ROUTING_MODE = os.environ.get("THROTTLE_API_KEY_ROUTING", "off").strip().lower()
API_KEY_ROUTING_MODE = (
    _API_KEY_ROUTING_MODE if _API_KEY_ROUTING_MODE in {"off", "overflow", "prefer"} else "off"
)
API_KEY_LABEL = os.environ.get("THROTTLE_API_KEY_LABEL", "API").strip() or "API"
API_KEY_MAX_CONCURRENT = max(
    1, int(os.environ.get("THROTTLE_API_KEY_MAX_CONCURRENT", str(MAX_CONCURRENT)))
)

# Optional fleet view: sibling proxies to cross-fetch on the dashboard so the
# whole fleet (e.g. the z.ai coding-plan proxy on :8766) shows in one pane.
# Format: "LABEL:http://host:port/__throttle/health,..." Parsed lazily by
# fleet.py — never touched on the hot path. Empty (default) hides the strip.
FLEET_HEALTH_URLS = os.environ.get("THROTTLE_FLEET_HEALTH", "")

# Optional GitHub Copilot panel: orgs + a classic PAT with read:org to read
# /orgs/{org}/copilot/billing. UI-only, TTL-cached, failure-tolerant. Empty
# (default) hides the panel. THROTTLE_COPILOT_TOKEN falls back to GITHUB_TOKEN
# so a shared gh-actions-style token works without duplication.
COPILOT_ORGS = os.environ.get("THROTTLE_COPILOT_ORGS", "")
COPILOT_TOKEN = (
    os.environ.get("THROTTLE_COPILOT_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
).strip()

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

# Stamped on every response this proxy serves (forwarding.stamp_proxy_marker,
# registered app-wide in proxy.main). A local tier receiving a 5xx from central
# uses its presence to tell "central alive, relaying an Anthropic 5xx" apart
# from "central layer itself dead" (dokku nginx answering for a dead
# container). Only the latter may mark central DOWN — marking down on relayed
# upstream blips stampedes the whole fleet into direct fallback, bypassing the
# central semaphore (05/07/2026 incident).
MARKER_HEADER = "x-anthropic-throttle-proxy"

# Stamped (alongside MARKER_HEADER) on the 503 this proxy GENERATES when a
# request exceeds QUEUE_MAX_WAIT_S waiting for a slot. A local tier that
# receives it from central must relay it to the client VERBATIM: it is
# admission backpressure from the proxy itself, not upstream pushback —
# pushback-retrying it would spin against the same full queue, and
# AIMD-shrinking on it would misattribute central's queue depth to this
# bearer's upstream behavior.
QUEUE_TIMEOUT_HEADER = "x-anthropic-throttle-queue-timeout"

# Request header carrying the REMAINING queue-wait budget (milliseconds) down
# the local→central chain. Without it the two tiers' QUEUE_MAX_WAIT_S bounds
# STACK — local waits 30 s, then central waits another 30 s, and the client
# has been silent for ~60 s, exactly the abort threshold the bound exists to
# stay under (Codex BLOCKER on PR #83). Each tier takes
# min(own knob, inherited budget) for its own queue wait and forwards what is
# left. Client-supplied values only shorten that client's own wait (min()
# can never exceed the local knob), so the header needs no trust filtering.
WAIT_BUDGET_HEADER = "x-anthropic-throttle-wait-budget-ms"

state: dict[str, object] = {
    "inflight": 0,
    "queued": 0,
    "served": 0,
    "client_disconnects": 0,
    "upstream_retries": 0,
    "central_status": "unknown",
    "central_last_check": 0,
    # Consecutive same-result probe counters backing the health hysteresis above.
    "central_consecutive_ok": 0,
    "central_consecutive_fail": 0,
    # Health serves this cached verdict; the DNS probe refreshes it in the
    # background so a slow container resolver cannot block the control plane.
    # Start optimistic until the first authoritative background result, matching
    # central target selection's cold-start policy.
    "upstream_egress_ok": True,
    "upstream_egress_error": "",
    "upstream_egress_last_check": 0,
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


# ---------------------------------------------------------------------------
# Runtime overrides (PR #22 — /ui/config editor)
# ---------------------------------------------------------------------------
#
# The values defined above are loaded once at import from the environment.
# The /ui dashboard exposes a small subset of these as *runtime-mutable* via a
# POST /ui/config endpoint: setting one writes the value to a JSON state file
# AND mutates the module attribute in-place (`config.MAX_CONCURRENT = N`,
# `body_shrink.CAP_BYTES = N`, `proxy.UTILIZATION_TARGET = N`, …). The hot
# paths read these knobs via attribute access (`config.MAX_CONCURRENT`,
# `if CAP_BYTES <= 0`, `if UTILIZATION_TARGET > 0`), so the mutation is picked
# up by every subsequent request without restart.
#
# Knobs that CANNOT be hot-mutated (because they change wire topology —
# UPSTREAM, CENTRAL_URL, LISTEN_HOST/PORT, QUEUE_MODE) stay env-only and the
# UI shows them as `restart required`.

import json as _json  # noqa: E402 — kept inline for visibility of new surface
from pathlib import Path as _Path  # noqa: E402
from typing import Any as _Any  # noqa: E402


def _effective_existing_limiter_hard_max() -> int:
    """Current hard cap for existing limiters under the active topology."""
    if QUEUE_MODE == "off" and CENTRAL_URL:
        return max(1, min(CENTRAL_LOCAL_MAX_CONCURRENT, MAX_CONCURRENT))
    return MAX_CONCURRENT


def _schedule_existing_limiter_retune(*, live_floor: int | None = None) -> None:
    """Best-effort hot retune for already-allocated bearer limiters."""
    try:
        import asyncio
        import importlib

        loop = asyncio.get_running_loop()
        limiter_mod = importlib.import_module("anthropic_throttle_proxy.limiter")
        loop.create_task(
            limiter_mod.retune_existing_limiters(
                _effective_existing_limiter_hard_max(),
                live_floor=live_floor,
            )
        )
    except RuntimeError:
        # No running event loop during import/tests; the next request will retune
        # through limiter._get_bearer_limiter.
        return


def _state_dir() -> _Path:
    """``$XDG_STATE_HOME/anthropic-throttle-proxy`` (or its fallback)."""
    base = os.environ.get("XDG_STATE_HOME") or str(_Path.home() / ".local" / "state")
    return _Path(base) / "anthropic-throttle-proxy"


OVERRIDES_FILE = _state_dir() / "overrides.json"

# Persisted /api/oauth/usage cache (UI accounts panel). In-memory only, a proxy
# restart wipes it — so a just-restarted proxy whose account is budget-locked
# and un-routed cold-polls usage, 429s, and renders the account fully blank
# (no fresh endpoint reading, no proxy-unified fallback). Persisting the last
# usage numbers keyed by credential PATH (never the token — invariant #2) lets
# the panel fall back to an AGED reading across restarts. Zero-config default;
# an override mirrors RETRY_AFTER_STATE_FILE for hosts that pin an XDG path.
ENDPOINT_CACHE_FILE = (
    _Path(os.path.expanduser(os.environ.get("THROTTLE_ENDPOINT_CACHE_FILE", "").strip()))
    if os.environ.get("THROTTLE_ENDPOINT_CACHE_FILE", "").strip()
    else _state_dir() / "endpoint-cache.json"
)

# ENV_DEFAULTS snapshots the env-derived value of every editable knob at
# import time so the UI can show "ENV default vs runtime override" and offer
# a reset button. Populated below after the knob schema is declared.
ENV_DEFAULTS: dict[str, _Any] = {}

# RUNTIME_OVERRIDES tracks what's been changed via the UI since startup.
# Empty == every knob still on its ENV_DEFAULTS value.
RUNTIME_OVERRIDES: dict[str, _Any] = {}


def _set_module_attr(module_path: str, attr: str, value: _Any) -> None:
    """``setattr(<module>, attr, value)`` with lazy import to avoid cycles."""
    import importlib

    mod = importlib.import_module(module_path)
    setattr(mod, attr, value)


# Fully-qualified module names for the runtime monkeypatch targets, named once
# so SonarQube python:S1192 (duplicated string literal) stays clean. f-strings
# keep the bare dotted literal out of the source (only ".config" etc. appear).
_PKG = "anthropic_throttle_proxy"
_MOD_CONFIG = f"{_PKG}.config"
_MOD_BODY_SHRINK = f"{_PKG}.body_shrink"
_MOD_PROXY = f"{_PKG}.proxy"


def _set_max_concurrent(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "MAX_CONCURRENT", v)
    _schedule_existing_limiter_retune()


def _set_min_dispatch_gap_ms(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "MIN_DISPATCH_GAP_S", float(v) / 1000.0)


def _set_central_local_max_concurrent(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "CENTRAL_LOCAL_MAX_CONCURRENT", v)
    _schedule_existing_limiter_retune()


def _set_aimd_min(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "AIMD_MIN", v)
    _schedule_existing_limiter_retune(live_floor=v)


def _schedule_limiter_kick() -> None:
    """Best-effort dispatch kick for already-allocated bearer limiters.

    Queued waiters only wake on acquire/release events; a retune that changes
    dispatch math (reserve raised, or lowered to 0 which migrates parked lane
    waiters to the normal queue) must kick the loop itself or those futures
    sit stranded until unrelated traffic arrives.
    """
    try:
        import asyncio
        import importlib

        loop = asyncio.get_running_loop()
        limiter_mod = importlib.import_module("anthropic_throttle_proxy.limiter")
        loop.create_task(limiter_mod.kick_existing_limiters())
    except RuntimeError:
        # No running event loop during import/tests; the next request will
        # re-enter _try_dispatch anyway.
        return


def _set_priority_reserve_slots(v: int) -> None:
    # Dispatch math reads this live; kick so already-parked waiters re-evaluate
    # (raise → lane dispatches now; 0 → parked lane waiters migrate to normal).
    _set_module_attr(_MOD_CONFIG, "PRIORITY_RESERVE_SLOTS", v)
    _schedule_limiter_kick()


def _set_priority_max_tokens(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "PRIORITY_MAX_TOKENS", v)


def _set_priority_max_body_bytes(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "PRIORITY_MAX_BODY_BYTES", v)


def _set_aimd_initial_concurrent(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "AIMD_INITIAL_CONCURRENT", v)
    _schedule_existing_limiter_retune(live_floor=v)


def _set_aimd_backoff_s(v: float) -> None:
    _set_module_attr(_MOD_CONFIG, "AIMD_BACKOFF_S", v)


def _set_concurrency_cooldown_s(v: float) -> None:
    _set_module_attr(_MOD_CONFIG, "CONCURRENCY_COOLDOWN_S", v)


def _set_aimd_ramp_after(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "AIMD_RAMP_AFTER", v)


def _set_aimd_ramp_after_fast(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "AIMD_RAMP_AFTER_FAST", v)


def _set_aimd_storm_threshold(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "AIMD_STORM_THRESHOLD", v)


def _set_aimd_decrease(v: float) -> None:
    _set_module_attr(_MOD_CONFIG, "AIMD_DECREASE", v)


def _set_max_hold_retry_after_s(v: float) -> None:
    _set_module_attr(_MOD_CONFIG, "MAX_HOLD_RETRY_AFTER_S", v)


def _set_queue_max_wait_s(v: float) -> None:
    _set_module_attr(_MOD_CONFIG, "QUEUE_MAX_WAIT_S", v)


def _set_body_shrink_cap_bytes(v: int) -> None:
    _set_module_attr(_MOD_BODY_SHRINK, "CAP_BYTES", v)


def _set_body_shrink_keep_turns(v: int) -> None:
    _set_module_attr(_MOD_BODY_SHRINK, "KEEP_TURNS", max(2, v))


def _set_body_shrink_min_block_bytes(v: int) -> None:
    _set_module_attr(_MOD_BODY_SHRINK, "MIN_BLOCK_BYTES", v)


def _set_utilization_target(v: float) -> None:
    _set_module_attr(_MOD_PROXY, "UTILIZATION_TARGET", v)


def _set_advisor_enabled(v: bool) -> None:
    os.environ["ADVISOR_ENABLED"] = "true" if v else "false"
    _set_module_attr(_MOD_PROXY, "ADVISOR_ENABLED", bool(v))


def _set_keepalive_hold(v: bool) -> None:
    _set_module_attr(_MOD_CONFIG, "KEEPALIVE_HOLD", bool(v))


def _set_keepalive_interval_ms(v: int) -> None:
    _set_module_attr(_MOD_CONFIG, "KEEPALIVE_INTERVAL_MS", max(500, v))


# EDITABLE_KNOBS is the single source of truth the UI consumes. Each entry:
#   key:          identifier in URLs, form fields, state file
#   label:        operator-facing name
#   `type`:       "int" | "float" | "bool"  (parser + input type)
#   min/max:      validation bounds (None = unbounded)
#   getter:       returns current effective value (env default OR runtime override)
#   setter:       mutates the right module attr; called after override-store update
#   units/help:   tooltip text + suffix in the form
EDITABLE_KNOBS: dict[str, dict[str, _Any]] = {
    "max_concurrent": {
        "label": "Max concurrent (per bearer)",
        "type": "int",
        "min": 1,
        "max": 512,
        "getter": lambda: MAX_CONCURRENT,
        "setter": _set_max_concurrent,
        "units": "slots",
        "help": (
            "Hard ceiling on in-flight requests per bearer when queueMode is "
            "'fair'/'reactive', or when central is down and the proxy falls "
            "back direct. AIMD shrinks the live cap below this on 429/503 "
            "pushback. In queueMode='off' + central set, this is shadowed "
            "by 'central_local_max_concurrent' (which is the actual binding "
            "cap). Suggested: 3 for Opus-heavy Claude Code traffic; raise "
            "only after central logs stay clean above that."
        ),
    },
    "min_dispatch_gap_ms": {
        "label": "Min dispatch gap",
        "type": "int",
        "min": 0,
        "max": 10000,
        "getter": lambda: int(MIN_DISPATCH_GAP_S * 1000),
        "setter": _set_min_dispatch_gap_ms,
        "units": "ms",
        "help": (
            "Process-global minimum gap between consecutive upstream POSTs "
            "(burst pacing). Orthogonal to concurrency caps — paces RATE, "
            "not parallelism. Suggested: 0 on local (no rate cap), 50 on "
            "central (smooth fleet-wide bursts). Raise only if Anthropic "
            "starts 429ing despite headroom on concurrency."
        ),
    },
    "central_local_max_concurrent": {
        "label": "Central fallback local cap",
        "type": "int",
        "min": 1,
        "max": 512,
        "getter": lambda: CENTRAL_LOCAL_MAX_CONCURRENT,
        "setter": _set_central_local_max_concurrent,
        "units": "slots",
        "help": (
            "THE binding per-bearer cap when queueMode='off' + a central "
            "URL is set: same-host Claude Code bursts share this small fair "
            "queue before egress to central (or direct fallback). Doubles "
            "as the direct-fallback cap when central is down. Suggested: "
            "3 for Opus-heavy Claude Code traffic; lower if Anthropic still "
            "returns 429s."
        ),
    },
    "priority_reserve_slots": {
        "label": "Priority reserve slots",
        "type": "int",
        "min": 0,
        "max": 16,
        "getter": lambda: PRIORITY_RESERVE_SLOTS,
        "setter": _set_priority_reserve_slots,
        "units": "slots",
        "help": (
            "Dedicated dispatch pool for short/latency-sensitive calls "
            "(e.g. Stop-hook evaluators: small max_tokens, no tools, small "
            "body). Independent of the main AIMD pool, so evaluators never "
            "starve behind long generations — total upstream concurrency is "
            "bounded by max_concurrent + this. 0 disables the lane."
        ),
    },
    "priority_max_tokens": {
        "label": "Priority max_tokens cutoff",
        "type": "int",
        "min": 0,
        "max": 65536,
        "getter": lambda: PRIORITY_MAX_TOKENS,
        "setter": _set_priority_max_tokens,
        "units": "tokens",
        "help": (
            "A request classifies as priority only when its JSON body has "
            "0 < max_tokens ≤ this, carries no tools, and the body is under "
            "the priority body-size cutoff. 8192 matches claude -p defaults."
        ),
    },
    "priority_max_body_bytes": {
        "label": "Priority body-size cutoff",
        "type": "int",
        "min": 0,
        "max": 33554432,
        "getter": lambda: PRIORITY_MAX_BODY_BYTES,
        "setter": _set_priority_max_body_bytes,
        "units": "bytes",
        "help": (
            "Requests with bodies larger than this never enter the priority "
            "lane, so a giant no-tools generation that happens to set a small "
            "max_tokens cannot jump the queue. Default 262144 (256 KiB) "
            "clears Stop-hook evaluator prompts with margin."
        ),
    },
    "utilization_target": {
        "label": "Utilization target",
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "getter": lambda: _get_proxy_attr("UTILIZATION_TARGET", 0.0),
        "setter": _set_utilization_target,
        "units": "(0=off)",
        "help": (
            "Smoothness lever for Claude Max OAuth bearers. When the "
            "binding 5h/7d unified-window utilisation crosses this "
            "fraction, the proxy proactively shrinks the live cap BEFORE "
            "a hard window block lands. Set to 0.85 to glide into the "
            "last 15% of window budget instead of slamming into a wall. "
            "0.0 disables (legacy default)."
        ),
    },
    "aimd_min": {
        "label": "AIMD floor",
        "type": "int",
        "min": 1,
        "max": 512,
        "getter": lambda: AIMD_MIN,
        "setter": _set_aimd_min,
        "units": "slots",
        "help": (
            "Floor under multiplicative-decrease shrink — live cap NEVER "
            "drops below this even after sustained 429 storm. Constitution "
            "III backstops env values below 1 via a clamp. Suggested: 8 "
            "on Max-tier so a sub-agent swarm survives pushback without "
            "collapsing to single-slot serialisation."
        ),
    },
    "aimd_initial_concurrent": {
        "label": "AIMD initial cap",
        "type": "int",
        "min": 1,
        "max": 512,
        "getter": lambda: AIMD_INITIAL_CONCURRENT,
        "setter": _set_aimd_initial_concurrent,
        "units": "slots",
        "help": (
            "Live cap assigned to a newly seen bearer before the proxy has "
            "evidence for that account's current safe parallelism. Set this "
            "low so account switches and service restarts start conservatively; "
            "AIMD then grows the cap after clean 2xx responses. Suggested: 1 "
            "on central, 1-2 on local direct fallback."
        ),
    },
    "aimd_backoff_s": {
        "label": "AIMD cooldown",
        "type": "float",
        "min": 0.0,
        "max": 3600.0,
        "getter": lambda: AIMD_BACKOFF_S,
        "setter": _set_aimd_backoff_s,
        "units": "s",
        "help": (
            "After a shrink, wait this many seconds before additive-"
            "increase can resume. Suggested: 5s on Max-tier (fast "
            "recovery, Anthropic's pushback windows are short). 30s is "
            "the conservative default for API-key tiers with stricter "
            "per-key rate limits."
        ),
    },
    "concurrency_cooldown_s": {
        "label": "Concurrency 429 cooldown",
        "type": "float",
        "min": 0.0,
        "max": 60.0,
        "getter": lambda: CONCURRENCY_COOLDOWN_S,
        "setter": _set_concurrency_cooldown_s,
        "units": "s",
        "help": (
            "Pause applied to a 429/503 that carries no Retry-After AND whose "
            "unified budget windows are still 'allowed' below the warn line — "
            "i.e. concurrency/rate pushback, not budget exhaustion. AIMD shrink "
            "already sheds the load; this only lets inflight drain. Keep it "
            "small (2s). Budget soft-throttles still get the full AIMD cooldown."
        ),
    },
    "aimd_ramp_after": {
        "label": "AIMD slow ramp threshold",
        "type": "int",
        "min": 1,
        "max": 10000,
        "getter": lambda: AIMD_RAMP_AFTER,
        "setter": _set_aimd_ramp_after,
        "units": "successes",
        "help": (
            "Consecutive 200s past the cooldown before live cap grows by "
            "+1, applied during STORM mode (≥aimd_storm_threshold shrinks "
            "inside the 2× aimd_backoff_s window). Isolated transient shrinks "
            "recover via "
            "aimd_ramp_after_fast instead. Suggested: 10 (current default — "
            "patient under sustained pushback)."
        ),
    },
    "aimd_ramp_after_fast": {
        "label": "AIMD fast ramp threshold",
        "type": "int",
        "min": 1,
        "max": 10000,
        "getter": lambda: AIMD_RAMP_AFTER_FAST,
        "setter": _set_aimd_ramp_after_fast,
        "units": "successes",
        "help": (
            "Consecutive 200s before live cap grows by +1, applied after an "
            "ISOLATED shrink (fewer than aimd_storm_threshold shrinks in the "
            "last 2× aimd_backoff_s window). Should be < aimd_ramp_after — the whole "
            "point of the adaptive ramp is to recover fast from a single 429 "
            "blip, while keeping the slow ramp for actual storms. Suggested: "
            "5 (≈ half the storm-mode tail latency)."
        ),
    },
    "aimd_storm_threshold": {
        "label": "AIMD storm threshold",
        "type": "int",
        "min": 1,
        "max": AIMD_STORM_THRESHOLD_MAX,
        "getter": lambda: AIMD_STORM_THRESHOLD,
        "setter": _set_aimd_storm_threshold,
        "units": "shrinks",
        "help": (
            "Shrink count inside the 2× aimd_backoff_s window that promotes "
            "ramp behaviour from FAST to SLOW. ≥N = sustained storm = slow ramp; "
            "otherwise = "
            "isolated transient = fast ramp. STORM_THRESHOLD=1 disables the "
            "adaptive path entirely (every shrink is treated as a storm). "
            "Suggested: 3."
        ),
    },
    "aimd_decrease": {
        "label": "AIMD shrink factor",
        "type": "float",
        "min": 0.1,
        "max": 0.95,
        "getter": lambda: AIMD_DECREASE,
        "setter": _set_aimd_decrease,
        "units": "x",
        "help": (
            "Multiplicative-decrease factor on 429/503. 0.5 = TCP Reno "
            "(aggressive halving, deep sawtooth). 0.7 = CUBIC (default, "
            "shallower sawtooth, higher average utilisation). 0.9+ "
            "UNDER-REACTS to real overload → cascade of 429s → user-"
            "visible stalls (research: Netflix concurrency-limits). "
            "Stick to 0.7 unless you have evidence to move."
        ),
    },
    "max_hold_retry_after_s": {
        "label": "Max held Retry-After",
        "type": "float",
        "min": 0.0,
        "max": 3600.0,
        "getter": lambda: MAX_HOLD_RETRY_AFTER_S,
        "setter": _set_max_hold_retry_after_s,
        "units": "s",
        "help": (
            "Largest upstream Retry-After window the proxy will hold a live "
            "client request open for before retrying. Longer windows are "
            "recorded for the bearer, then requests fail fast with 429 so "
            "Claude sees the real rate-limit state instead of timing out "
            "behind the local gateway. Default 60s holds Anthropic's common "
            "short 30s temporary throttles while still rejecting multi-hour "
            "account-window waits."
        ),
    },
    "queue_max_wait_s": {
        "label": "Max queue wait",
        "type": "float",
        "min": 0.0,
        "max": 600.0,
        "getter": lambda: QUEUE_MAX_WAIT_S,
        "setter": _set_queue_max_wait_s,
        "units": "s",
        "help": (
            "Longest a request may wait in the fair queue for a slot before "
            "the proxy fails fast with 503 + Retry-After. Claude Code aborts "
            "a silent request at ~60s; a response written after that hits a "
            "closing transport and surfaces as InvalidHTTPResponse (phantom "
            "401). Keep this inside the client's patience window (~30s) so "
            "the SDK retries transparently instead. 0 disables the bound."
        ),
    },
    "body_shrink_cap_bytes": {
        "label": "Body-shrink cap",
        "type": "int",
        "min": 0,
        "max": 33_554_432,  # 32 MiB upper bound matches Anthropic's hard cap
        "getter": lambda: _get_body_shrink_attr("CAP_BYTES", 0),
        "setter": _set_body_shrink_cap_bytes,
        "units": "bytes",
        "help": (
            "Soft cap below which body_shrink does not trim. 0 disables "
            "the whole feature. Bodies above this size get older "
            "tool_result blocks dropped (preserves the last "
            "'keep_turns' messages) before they hit Anthropic's hard "
            "32 MiB cap and 413 your request."
        ),
    },
    "body_shrink_keep_turns": {
        "label": "Body-shrink keep turns",
        "type": "int",
        "min": 2,
        "max": 64,
        "getter": lambda: _get_body_shrink_attr("KEEP_TURNS", 4),
        "setter": _set_body_shrink_keep_turns,
        "units": "msgs",
        "help": (
            "Trailing messages left untouched by the trimmer — the most "
            "recent N user/assistant turns always survive body-shrink. "
            "Raise if you see the model losing recent context after "
            "shrink fires."
        ),
    },
    "body_shrink_min_block_bytes": {
        "label": "Body-shrink min block",
        "type": "int",
        "min": 0,
        "max": 1_048_576,
        "getter": lambda: _get_body_shrink_attr("MIN_BLOCK_BYTES", 2048),
        "setter": _set_body_shrink_min_block_bytes,
        "units": "bytes",
        "help": (
            "Skip trimming tool_result blocks whose serialised size is "
            "below this threshold — saves you trimming tiny noise blocks "
            "where the savings are negligible but the readability loss "
            "is real."
        ),
    },
    "keepalive_hold": {
        "label": "SSE keepalive-hold",
        "type": "bool",
        "getter": lambda: KEEPALIVE_HOLD,
        "setter": _set_keepalive_hold,
        "help": (
            "When true, a streaming POST /v1/messages that hits a TRANSIENT "
            "throttle (529/concurrency-429/central-queue-503) receives a 200 "
            "text/event-stream immediately with SSE ':' keepalive comments "
            "while the proxy retries internally, so the SDK sees a slow "
            "response rather than a retry banner. Budget-rejected / long "
            "Retry-After / non-streaming requests keep the clean-error path. "
            "False = legacy pass-through behavior."
        ),
    },
    "keepalive_interval_ms": {
        "label": "Keepalive interval",
        "type": "int",
        "min": 500,
        "max": 30000,
        "getter": lambda: KEEPALIVE_INTERVAL_MS,
        "setter": _set_keepalive_interval_ms,
        "units": "ms",
        "help": (
            "Interval between SSE ':' keepalive comment frames during a hold "
            "(spec 092). Must be well under the client idle-timeout (~60 s); "
            "default 10 s gives 6 heartbeats before the most aggressive abort. "
            "Minimum 500 ms enforced to prevent tight-loop writes."
        ),
    },
    "advisor_enabled": {
        "label": "GROQ advisor",
        "type": "bool",
        "getter": lambda: os.environ.get("ADVISOR_ENABLED", "false").strip().lower() == "true",
        "setter": _set_advisor_enabled,
        "help": (
            "Auto-fire a GROQ diagnosis on 429/503/529 events (debounced) "
            "and surface it under 'Latest auto-diagnosis'. Also enables "
            "the on-demand 'Ask advisor' button. Requires GROQ_API_KEY in "
            "the EnvironmentFile (~/.local/state/anthropic-throttle-proxy/"
            "groq.env)."
        ),
    },
}


def _get_body_shrink_attr(name: str, default: _Any) -> _Any:
    """Read a body_shrink module attr defensively (module may not be imported yet)."""
    try:
        import importlib

        mod = importlib.import_module(_MOD_BODY_SHRINK)
        return getattr(mod, name, default)
    except Exception:
        return default


def _get_proxy_attr(name: str, default: _Any) -> _Any:
    """Read a proxy module attr defensively."""
    try:
        import importlib

        mod = importlib.import_module(_MOD_PROXY)
        return getattr(mod, name, default)
    except Exception:
        return default


def _capture_env_defaults() -> None:
    """Snapshot each knob's current effective value before any overrides apply."""
    for key, spec in EDITABLE_KNOBS.items():
        try:
            ENV_DEFAULTS[key] = spec["getter"]()
        except Exception:
            ENV_DEFAULTS[key] = None


def _coerce(spec: dict[str, _Any], raw: _Any) -> _Any:
    """Parse a raw form/JSON value into the declared type, with bounds check."""
    t = spec["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"true", "1", "on", "yes"}
    if t == "int":
        v = int(raw)
    elif t == "float":
        v = float(raw)
    else:
        raise ValueError(f"unsupported knob type: {t!r}")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        raise ValueError(f"{spec['label']}: value {v} below min {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{spec['label']}: value {v} above max {hi}")
    return v


def set_override(key: str, raw_value: _Any) -> _Any:
    """Validate, persist, and propagate a single knob override.

    Returns the coerced value that's now live. Raises ``KeyError`` for unknown
    keys and ``ValueError`` for type / bounds violations.
    """
    if key not in EDITABLE_KNOBS:
        raise KeyError(f"unknown knob: {key!r}")
    spec = EDITABLE_KNOBS[key]
    value = _coerce(spec, raw_value)
    RUNTIME_OVERRIDES[key] = value
    spec["setter"](value)
    save_overrides()
    log(f"config override set: {key}={value} (was env default {ENV_DEFAULTS.get(key)})")
    return value


def reset_override(key: str) -> _Any:
    """Drop a runtime override; restore the env default value. Returns the restored value."""
    if key not in EDITABLE_KNOBS:
        raise KeyError(f"unknown knob: {key!r}")
    spec = EDITABLE_KNOBS[key]
    RUNTIME_OVERRIDES.pop(key, None)
    default = ENV_DEFAULTS.get(key)
    if default is not None:
        spec["setter"](default)
    save_overrides()
    log(f"config override reset: {key} → env default {default}")
    return default


def load_overrides() -> None:
    """Read the on-disk overrides file (if any) and re-apply each entry.

    Called once from the proxy entrypoint *after* every module that owns an
    editable attr has been imported, so the setters can find the targets.
    Missing or unreadable file is a no-op (all knobs stay on env defaults).
    """
    _capture_env_defaults()
    if not OVERRIDES_FILE.is_file():
        return
    try:
        data = _json.loads(OVERRIDES_FILE.read_text())
    except Exception as exc:
        log(f"config: cannot read {OVERRIDES_FILE} ({exc!r}); skipping overrides")
        return
    if not isinstance(data, dict):
        log(f"config: {OVERRIDES_FILE} not a JSON object; skipping")
        return
    for key, raw_value in data.items():
        if key not in EDITABLE_KNOBS:
            log(f"config: ignoring unknown override key {key!r}")
            continue
        try:
            value = _coerce(EDITABLE_KNOBS[key], raw_value)
        except ValueError as exc:
            log(f"config: ignoring invalid override {key}={raw_value!r} ({exc})")
            continue
        RUNTIME_OVERRIDES[key] = value
        EDITABLE_KNOBS[key]["setter"](value)
    log(f"config: loaded {len(RUNTIME_OVERRIDES)} override(s) from {OVERRIDES_FILE}")


def save_overrides() -> None:
    """Persist RUNTIME_OVERRIDES to disk. Best-effort; logs on failure."""
    try:
        OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDES_FILE.write_text(_json.dumps(RUNTIME_OVERRIDES, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        log(f"config: cannot persist overrides to {OVERRIDES_FILE} ({exc!r})")


def knob_snapshot() -> list[dict[str, _Any]]:
    """Render each editable knob as a row for the UI form.

    Each row carries enough fields to fully render itself (label, type, value,
    default, override flag, help text, units, min/max bounds).
    """
    rows: list[dict[str, _Any]] = []
    for key, spec in EDITABLE_KNOBS.items():
        try:
            current = spec["getter"]()
        except Exception:
            current = None
        rows.append(
            {
                "key": key,
                "label": spec["label"],
                "type": spec["type"],
                "value": current,
                "default": ENV_DEFAULTS.get(key),
                "override": key in RUNTIME_OVERRIDES,
                "help": spec.get("help", ""),
                "units": spec.get("units", ""),
                "min": spec.get("min"),
                "max": spec.get("max"),
            }
        )
    return rows
