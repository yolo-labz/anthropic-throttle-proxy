"""Unified ``:8760`` ingress — the "never run out of AI" router.

Spec 093. A single Anthropic-shape aiohttp server every claude-code tab points
at. It routes each request across the per-lane throttles (``:8765`` Anthropic,
``:8766`` z.ai-GLM, ``:8767`` Kimi) by role + live gauges so the fleet degrades
gracefully and never hard-fails for lack of a model.

**S1 scope (this file today):** ingress skeleton + no-op-when-unset. The
ingress forwards every request to a configured default lane
(``INGRESS_DEFAULT_LANE_URL``) path-preservingly, byte-identical to pointing the
client at the lane directly. Role inference (S2), gauge-driven lane selection
(S3), model-remap (S4), the never-hard-fail / no-silent-downgrade guards (S5),
and observability (S6) layer on later without changing this forward shape.

The ingress is opt-in: it is a separate process the operator starts on
``:8760``. With it unset, claude-code points at ``:8765`` as today (invariant
5, zero behavior change). The per-lane proxies stay individually reachable as
the SPOF fallback if this router dies.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from typing import Final

import aiohttp
from aiohttp import web
from prometheus_client import CollectorRegistry, Counter, generate_latest

from . import routing
from .routing import (
    Lane,
    LaneState,
    body_has_tools,
    code_role_rejection_reason,
    default_lanes,
    infer_role_from_body,
    lane_usable,
    remap_body_model,
    select_lane,
    session_key_from_body,
)

# --- config (env-derived, read once at import like config.py) ----------------
# The ingress listens on its own port; 127.0.0.1 keeps it host-local (the fleet
# is same-host claude-code tabs; a remote ingress would re-add the SPOF the
# per-lane proxies remove).
INGRESS_HOST: Final[str] = os.environ.get("INGRESS_HOST", "127.0.0.1")
INGRESS_PORT: Final[int] = int(os.environ.get("INGRESS_PORT", "8760"))

# The lane the ingress forwards to in S1 passthrough mode. Defaults to the
# Anthropic lane (:8765) so the ingress is a no-op until S3 adds gauge-driven
# selection.
DEFAULT_LANE_URL: Final[str] = os.environ.get(
    "INGRESS_DEFAULT_LANE_URL", "http://127.0.0.1:8765"
).rstrip("/")

# Upstream total timeout for a forwarded turn. Default 0 = NO total cap: the
# ingress forwards to a per-lane throttle which already enforces its own
# sock-read bound (PR #130 / NixOS #1327), so layering a tighter total cap
# here would kill legit long generations the lane is willing to serve. Set
# lower only for a stall-prone lane where the lane's own sock-read is too loose.
FORWARD_TIMEOUT_S: Final[float] = float(os.environ.get("INGRESS_FORWARD_TIMEOUT_S", "0"))

# S2: maximum request-body bytes inspected for the ``model`` field on
# POST /v1/messages. The model is early in a claude-code body, so 64 KiB is
# ample; bodies larger than this keep streaming through unparsed (role defaults
# to generate) — bounds memory + the json.loads CPU surface (gate BLOCKER).
ROLE_BODY_READ_LIMIT: Final[int] = int(
    os.environ.get("INGRESS_ROLE_BODY_READ_LIMIT", str(64 * 1024))
)

# S4: max request-body bytes buffered for model-remap on POST /v1/messages to a
# non-Anthropic lane (remap needs the full body to re-serialize). The hot
# Anthropic path never buffers beyond ROLE_BODY_READ_LIMIT (no remap); only the
# Kimi/GLM overflow path reads up to this cap. Bodies larger skip remap and
# stream verbatim (the lane may reject) — bounds memory on the overflow path.
REMAP_BODY_MAX_BYTES: Final[int] = int(
    os.environ.get("INGRESS_REMAP_BODY_MAX_BYTES", str(8 * 1024 * 1024))
)

# Stamp on every served response so a downstream tier / probe can tell an
# ingress-served response from a direct-lane response.
MARKER_HEADER: Final[str] = "x-anthropic-throttle-ingress"

# S2: the inferred role (generate/judge/bulk) stamped on served RESPONSES for
# observability. S6 surfaces per-(role→lane) decision counts. This is a response
# stamp only — never read from the request (see ROLE_OVERRIDE_HEADER).
ROLE_HEADER: Final[str] = "x-anthropic-throttle-role"

# A DISTINCT REQUEST header a trusted consumer sets to override model-tier role
# inference (only bulk/judge honored — see routing.role_from_header). Kept
# separate from ROLE_HEADER (a response stamp) so the two directions never
# alias, and STRIPPED in _forward_headers so a client-set value never
# propagates to the upstream lane — mirrors the repo's anti-spoof posture on
# x-anthropic-throttle-wait-budget-ms (GLM finding B).
ROLE_OVERRIDE_HEADER: Final[str] = "x-anthropic-throttle-role-hint"

# S2: the lane id the ingress routed to, stamped on served responses.
LANE_HEADER: Final[str] = "x-anthropic-throttle-lane"

# ADR-6a (Fleet Foundry K3): opt-in request-scoped credential-mode enforcement.
# A caller demands the subscription-only contract with:
#   x-anthropic-throttle-require-credential-mode: subscription
# The header is consumed here, never forwarded. Constrained 2xx responses are
# stamped ``credential-mode: subscription``; the 403 refusal carries ``unknown``
# (r1/C3 — unconstrained responses carry no stamp; the full four-value
# vocabulary is normative for per-lane health only). Both response headers are
# reserved: any upstream/client-supplied value is stripped before relay
# (anti-spoof).
REQUIRE_CREDENTIAL_MODE_HEADER: Final[str] = "x-anthropic-throttle-require-credential-mode"
CREDENTIAL_MODE_HEADER: Final[str] = "x-anthropic-throttle-credential-mode"
CREDENTIAL_MODE_REASON_HEADER: Final[str] = "x-anthropic-throttle-credential-mode-reason"
CREDENTIAL_MODE_SUBSCRIPTION: Final[str] = "subscription"
CREDENTIAL_MODE_DIRECT_KEY: Final[str] = "direct_key"
CREDENTIAL_MODE_PROXY_KEY: Final[str] = "proxy_key"
CREDENTIAL_MODE_UNKNOWN: Final[str] = "unknown"
# ADR-6a capability declaration; a consumer refuses an unknown contract.
ENFORCEMENT_CONTRACT: Final[str] = "adr6a-credential-mode/1"
# Refusal reason tokens (ADR-6a §3(c)) — policy verdict, never capacity.
REFUSAL_NO_ELIGIBLE_LANE: Final[str] = "no_eligible_lane"
REFUSAL_ELIGIBLE_EXHAUSTED: Final[str] = "eligible_lanes_exhausted"
# Fail-closed upstream allowlist (ADR-6a E2). Comma-separated hosts.
# Unset/empty => NO lane is eligible (invariant A: absence of policy is not
# permission). Example: "api.anthropic.com"
_SUBSCRIPTION_UPSTREAMS: Final[frozenset[str]] = frozenset(
    host.strip().lower()
    for host in os.environ.get("INGRESS_SUBSCRIPTION_UPSTREAMS", "").split(",")
    if host.strip()
)


# Saturation spill (coordinator ask): a sibling proxy lane stamps this on a 503
# from its OWN queue-wait timeout. The ingress detects it to SPILL the request to
# the next lane in the role chain instead of passing the 503 through. Only a
# sibling proxy sets it, so it's a trustworthy saturation signal.
QUEUE_TIMEOUT_HEADER: Final[str] = "x-anthropic-throttle-queue-timeout"
# How long the ingress avoids a lane that just returned a saturation 503. Covers
# ~one health-poll cycle; a draining lane re-opens on the next poll, a still-
# saturated lane re-marks on the next 503.
LANE_SATURATION_COOLDOWN_S: Final[float] = float(
    os.environ.get("INGRESS_LANE_SATURATION_COOLDOWN_S", "15")
)
# When a generate request hits a saturation-503 from the Anthropic lane and
# there's NO overflow lane (overflow disabled or generate-only chain), the
# ingress RETRIES the same lane this many times with a delay between each.
# This makes the ingress QUEUE-AND-WAIT (honoring the Anthropic lane's own
# queue) instead of 503-aborting — matching what direct :8765 does (the nix
# w1W:p4 flip-gate requirement). Total max wait = retries × delay (default
# 3 × 5 = 15s, well under claude-code's ~60s patience).
GENERATE_QUEUE_RETRIES: Final[int] = int(os.environ.get("INGRESS_GENERATE_QUEUE_RETRIES", "3"))
GENERATE_QUEUE_RETRY_DELAY_S: Final[float] = float(
    os.environ.get("INGRESS_GENERATE_QUEUE_RETRY_DELAY_S", "5")
)

# #182/#184: roles whose lane chain treats a bare upstream 429 (no stamped
# saturation header) as spillable rather than passing it straight through to
# the client. "generate" needs it for :8765's own leaked pushback (see the
# is_retryable comment below); "code" needs it because the Codex lane's
# ChatGPT-meter 429 carries no Retry-After/unified gauges the ingress can
# trust — same shape, same fix. bulk/judge are deliberately excluded: their
# lanes' own AIMD already absorbs 429s internally (see the proxy's per-lane
# pushback handling), and widening this to every role is a bigger behavior
# change than either #182 or #184 asked for.
_SPILL_ON_429_ROLES: Final[frozenset[str]] = frozenset({"generate", "code"})

# --- S3: lane registry + gauge polling --------------------------------------
# The three-lane fleet (Spec 093). Built once at import from env-overridable URLs.
LANES: dict[str, Lane] = default_lanes()

# Per-lane cached gauge verdict, updated by ``_lane_health_loop``. Read by
# ``select_lane`` (pure) on each forward. A lane missing from here is treated as
# not-yet-known → ``select_lane`` skips it (so cold-start forwards only after the
# initial poll completes in the cleanup_ctx, avoiding a 503 storm).
lane_state: dict[str, LaneState] = {}

# Poll cadence + probe timeout for the background lane-health loop. Mirrors the
# per-lane proxy's central_health_loop pattern (every Ns; short timeout).
LANE_HEALTH_INTERVAL_S: Final[float] = float(os.environ.get("INGRESS_LANE_HEALTH_INTERVAL_S", "5"))
LANE_HEALTH_TIMEOUT_S: Final[float] = float(os.environ.get("INGRESS_LANE_HEALTH_TIMEOUT_S", "2"))
# ADR-6a §1.3 request-time freshness bound: cached lane evidence older than
# this is treated as ``unknown``/stale at selection and in refusal counting,
# so a dead poll task cannot keep authorizing constrained work forever.
_CREDENTIAL_EVIDENCE_MAX_AGE_S: Final[float] = LANE_HEALTH_INTERVAL_S * 3 + 5
# Cap on a lane's /__throttle/health body before parsing (gate MAJOR: a
# misconfigured/compromised lane returning a huge JSON could exhaust memory).
# Lane URLs are config-only (no SSRF), but the parse is still bounded defensively.
LANE_HEALTH_MAX_BYTES: Final[int] = int(
    os.environ.get("INGRESS_LANE_HEALTH_MAX_BYTES", str(1024 * 1024))
)


async def _read_bounded(stream: aiohttp.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read up to ``limit`` body bytes. Returns ``(data, complete)``: ``complete``
    is True iff the whole body fit (genuine EOF reached within ``limit``).

    LOOPS ``read()`` to accumulate up to ``limit`` — a single ``StreamReader.read(n)``
    returns AS SOON AS any data is available (the first TCP segment / chunk), which
    can be FEWER than ``n`` bytes with more still coming. Treating that short read
    as EOF (the old ``len(data) < limit``) mis-flags a multi-chunk upload as
    complete, so ``remap_body_model`` runs on a TRUNCATED prefix → invalid JSON is
    re-serialized → Moonshot 400 "unexpected end of JSON input" (24/07 incident:
    claude-code sends its body in multiple chunks; curl sent it in one read, hence
    curl passed but real claude-code 400'd). A short read is NOT EOF — only an
    EMPTY read is. So loop until ``limit`` bytes OR a genuine empty read."""
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await stream.read(limit - total)
        if not chunk:  # empty read == genuine EOF
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    # Accumulated exactly ``limit`` bytes without hitting EOF — more may follow.
    return b"".join(chunks), stream.at_eof()


async def _chain_stream(stream: aiohttp.StreamReader, *initial: bytes) -> AsyncIterator[bytes]:
    """Yield the ``initial`` byte chunks, then drain ``stream`` — a byte-complete
    forward when the body was only partially buffered (large-body / no-remap path)."""
    for chunk in initial:
        if chunk:
            yield chunk
    async for chunk in stream.iter_any():
        if chunk:
            yield chunk


# S4 session stickiness: metadata.user_id → pinned lane id. Keeps a session on
# its lane across requests (cache economics — a mid-session switch forces a slow
# uncached turn). Evicted when the pinned lane goes closed (see _poll_one_lane).
_session_lane: dict[str, str] = {}

# S6 observability: a process-local Prometheus registry (NOT the proxy's — the
# ingress is a separate process). The route-decision counter is the S6 core
# signal; Kimi-balance polling is a documented follow-up (needs the Moonshot key
# + a dedicated poll loop).
REGISTRY = CollectorRegistry()
M_ROUTE_DECISIONS = Counter(
    "ingress_route_decisions_total",
    "Requests routed by the unified ingress, by inferred role and chosen lane.",
    ["role", "lane"],
    registry=REGISTRY,
)
# Why an AGENTIC request missed the "code" role and fell back to generate.
# Counting only agentic bodies keeps this about the code envelope: a plain
# chat turn has no business being reported as a near-miss. The label set is
# the fixed CODE_REJECT_* vocabulary, never request content, so cardinality is
# bounded. Without this, a zero ``code`` row gave no reason at all — measured
# 15/08/2026, finding it meant grepping the service journal. Read it ALONGSIDE
# ingress_route_decisions_total: this counter alone cannot separate "no agentic
# traffic" from "everything qualified", since both leave it at zero.
M_CODE_ROLE_REJECTED = Counter(
    "ingress_code_role_rejected_total",
    "Agentic requests that did NOT qualify for the code role, by reason.",
    ["reason"],
    registry=REGISTRY,
)

# Hop-by-hop headers (RFC 7230 §6.1) — must not be forwarded verbatim; aiohttp
# also manages Content-Length / Transfer-Encoding on the rebuilt request.
# ``content-length`` is filtered too: the ingress may rewrite the body (S4
# model-remap changes its length), so the client's CL must NOT be forwarded —
# aiohttp recomputes it from the bytes actually sent (or chunked for streams).
_HOP_BY_HOP: Final[frozenset[str]] = frozenset(
    {
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
)

# Start-time gauge: a step change = restart, the one durable restart signal
# (mirrors the per-lane proxy's M_START_TIME).
_start_time = time.time()
_served = 0

# Per-app ClientSession key (lifecycle-managed via cleanup_ctx so connections
# are reused across forwards and cleanly closed on shutdown).
_SESSION_KEY: Final[str] = web.AppKey("ingress_session")


# Ingress-internal request headers consumed HERE — never forwarded to the
# upstream lane. The role-hint override is admission-only; forwarding it verbatim
# would let it propagate through a chained/remote ingress and be re-applied as an
# override downstream (spoofing), the class the repo already gates for
# x-anthropic-throttle-wait-budget-ms (GLM finding B).
_INGRESS_ONLY_HEADERS: Final[frozenset[str]] = frozenset(
    {
        ROLE_OVERRIDE_HEADER,
        REQUIRE_CREDENTIAL_MODE_HEADER,
        # ADR-6a §2.1 anti-spoof: a client must not be able to assert its own
        # credential mode; strip these from the REQUEST before forwarding too.
        CREDENTIAL_MODE_HEADER,
        CREDENTIAL_MODE_REASON_HEADER,
    }
)
# Reserved credential response headers: stripped from ANY upstream response so
# a lane/provider cannot forge the receipt; the ingress authors the truth.
_RESERVED_CREDENTIAL_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {CREDENTIAL_MODE_HEADER, CREDENTIAL_MODE_REASON_HEADER}
)


def _forward_headers(request: web.Request) -> dict[str, str]:
    """Client headers minus hop-by-hop + ingress-internal, ready for upstream."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _INGRESS_ONLY_HEADERS
    }


def _require_credential_mode(request: web.Request) -> str | None:
    """ADR-6a request gate: ``subscription`` when the opt-in header demands it.

    Present-but-invalid fails closed (400) before body read / selection. Absent
    => None (legacy unconstrained path, behavior unchanged).
    """
    values = request.headers.getall(REQUIRE_CREDENTIAL_MODE_HEADER, [])
    if not values:
        return None
    if len(values) != 1 or values[0].strip().lower() != CREDENTIAL_MODE_SUBSCRIPTION:
        return web.json_response(
            {
                "type": "error",
                "error": {
                    "type": "unsupported_credential_requirement",
                    "message": (
                        "only x-anthropic-throttle-require-credential-mode: subscription "
                        "is supported"
                    ),
                },
            },
            status=400,
        )
    return CREDENTIAL_MODE_SUBSCRIPTION


def _bearer_reset_candidates(bearer: dict, now: float) -> list[float]:
    """Future reopening candidates (epoch) for one bearer's active blockers."""
    out: list[float] = []
    limiter = bearer.get("limiter")
    if isinstance(limiter, dict):
        retry_after_until = limiter.get("retry_after_until")
        if (
            isinstance(retry_after_until, int | float)
            and not isinstance(retry_after_until, bool)
            and float(retry_after_until) > now
        ):
            out.append(float(retry_after_until))
    unified = bearer.get("unified")
    if isinstance(unified, dict):
        for suffix in ("_5h", "_7d"):
            if unified.get(f"status{suffix}") != "rejected":
                continue
            reset = unified.get(f"reset{suffix}")
            if (
                isinstance(reset, int | float)
                and not isinstance(reset, bool)
                and float(reset) > now
            ):
                out.append(float(reset))
    return out


def _health_upstream_host(health_json: dict) -> str:
    """Hostname of the lane's health ``upstream`` URL, lowercased."""
    raw = health_json.get("upstream")
    if not isinstance(raw, str):
        return ""
    try:
        from urllib.parse import urlsplit

        return (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""


def _upstream_canonical_allowlisted(health_json: dict) -> bool:
    """r1/C4: E2 passes iff ``upstream`` is a CANONICAL allowlisted URL.

    scheme https, host ∈ allowlist, port ∈ {absent, 443}, path ∈ {"", "/"},
    no userinfo, query, or fragment. A host-only match would admit
    ``api.deepseek.com/anthropic`` if a pay-go host were ever allowlisted;
    this is defence in depth behind E1.
    """
    raw = health_json.get("upstream")
    if not isinstance(raw, str):
        return False
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in _SUBSCRIPTION_UPSTREAMS
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _lane_desktop_local(lane: Lane, health_json: dict) -> bool:
    """r1/C2: E4 — lane URL host is loopback AND health ``central_url == ""``.

    Loopback proves only the ingress→lane hop; a configured central relay
    would carry the credential off-desktop AFTER that hop, so the empty
    central URL is a first-class part of the desktop-locality signal.
    """
    try:
        from ipaddress import ip_address
        from urllib.parse import urlsplit

        parsed = urlsplit(lane.url)
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host.lower() == "localhost":
        loopback = True
    else:
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            return False
    return loopback and health_json.get("central_url") == ""


_WINDOW_STATUSES: Final[frozenset[str]] = frozenset({"allowed", "allowed_warning", "rejected"})


def _bearer_has_windows(bearer: dict) -> bool:
    unified = bearer.get("unified")
    if not isinstance(unified, dict):
        return False
    return (
        unified.get("status_5h") in _WINDOW_STATUSES
        and unified.get("status_7d") in _WINDOW_STATUSES
    )


def _classify_lane_class(lane: Lane, health_json: dict) -> tuple[str, str]:
    """r1/C1 CLASS level — E1 ∧ E2 ∧ E4. Returns ``(mode, reason)``.

    Governs the per-lane health ``credential_mode`` and refusal
    ``eligible_configured``. Does NOT include E3 (capacity); a capped
    subscription lane keeps CLASS ``subscription`` so ``eligible_lanes_exhausted``
    stays reachable. ``unknown`` (not absent) always pairs a stable reason.
    """
    if not isinstance(health_json, dict):
        return CREDENTIAL_MODE_UNKNOWN, "health-invalid"
    api_key = health_json.get("api_key")
    if not isinstance(api_key, dict) or not isinstance(api_key.get("enabled"), bool):
        return CREDENTIAL_MODE_UNKNOWN, "fields-absent"
    if api_key.get("enabled"):
        return CREDENTIAL_MODE_DIRECT_KEY, "api-key-enabled"
    if not _SUBSCRIPTION_UPSTREAMS:
        return CREDENTIAL_MODE_UNKNOWN, "no-upstream-allowlist"
    if not _upstream_canonical_allowlisted(health_json):
        host = _health_upstream_host(health_json)
        return CREDENTIAL_MODE_PROXY_KEY, f"upstream-not-allowlisted:{host or 'none'}"
    if not _lane_desktop_local(lane, health_json):
        return CREDENTIAL_MODE_PROXY_KEY, "not-desktop-local"
    return CREDENTIAL_MODE_SUBSCRIPTION, ""


def _classify_lane_capacity(health_json: dict, now: float | None = None) -> bool:
    """r1/C1 CAPACITY level — E3 (≥1 usable bearer with 5h/7d windows).

    Freshness (r1 §1.3 / precheck R3): a bearer's window sample must carry
    ``unified_at`` within the evidence age bound; a stale snapshot cannot make
    the lane selectable for a constrained request.
    """
    now = time.time() if now is None else now
    bearers = health_json.get("bearers")
    if not isinstance(bearers, dict):
        return False
    for bearer in bearers.values():
        if not isinstance(bearer, dict) or not _bearer_has_windows(bearer):
            continue
        observed_at = bearer.get("unified_at")
        if (
            not isinstance(observed_at, int | float)
            or isinstance(observed_at, bool)
            # Small negative tolerance: the probe's ``now`` is captured before
            # the lane stamps its response, so a sub-second skew is not a
            # future sample.
            or not (-1.0 <= now - float(observed_at) <= _CREDENTIAL_EVIDENCE_MAX_AGE_S)
        ):
            continue
        if routing.bearer_usable(bearer, now):
            return True
    return False


def _lane_mode_from_state(state: LaneState | None) -> tuple[str, str]:
    """Mode + reason cached on a polled ``LaneState`` (see ``_poll_one_lane``)."""
    if state is None:
        return CREDENTIAL_MODE_UNKNOWN, "lane-not-polled"
    return state.credential_mode, state.credential_mode_reason


def _set_lane_state(lane_id: str, *, open_: bool, detail: str) -> None:
    """Request-time availability transition preserving the polled credential
    class (mode/reason/reset) — the poll is the only authority for those."""
    current = lane_state.get(lane_id)
    if current is None:
        lane_state[lane_id] = LaneState(open_, time.time(), detail)
        return
    lane_state[lane_id] = LaneState(
        open_,
        time.time(),
        detail,
        credential_mode=current.credential_mode,
        credential_mode_reason=current.credential_mode_reason,
        credential_capacity_ok=current.credential_capacity_ok,
        credential_reset_at=current.credential_reset_at,
    )


def _lane_is_subscription(state: LaneState | None) -> bool:
    return state is not None and state.credential_mode == CREDENTIAL_MODE_SUBSCRIPTION


def _mode_is_usable_eligible(state: LaneState | None) -> bool:
    """Constrained selectability: CLASS subscription ∧ CAPACITY ∧ open."""
    return (
        state is not None
        and state.credential_mode == CREDENTIAL_MODE_SUBSCRIPTION
        and state.credential_capacity_ok
        and state.open
    )


def _evidence_is_fresh(state: LaneState, now: float) -> bool:
    """ADR-6a §1.3: cached evidence is only trustworthy within the age bound."""
    return 0 <= now - state.checked_at <= _CREDENTIAL_EVIDENCE_MAX_AGE_S


def _allowlist_digest() -> str:
    """r1/C6: sha256 over lowercased sorted comma-joined hosts, hex, [:12]."""
    if not _SUBSCRIPTION_UPSTREAMS:
        return ""
    import hashlib

    joined = ",".join(sorted(_SUBSCRIPTION_UPSTREAMS))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _policy_refusal(role: str, required_mode: str) -> web.Response:
    """ADR-6a §3: pre-egress 403 policy verdict with the two distinct reasons.

    ``no_eligible_lane``: no lane of the required class is configured or
    determinable (for THIS role's chain). ``eligible_lanes_exhausted``: an
    eligible lane exists but is capped/paused. Never 503: this is policy, not
    capacity, and must not enter AIMD/retry as pushback.
    """
    now = time.time()
    chain = routing.effective_chain(role, routing.GENERATE_OVERFLOW_ENABLED)
    chain_states = [st for lid in chain if lid in LANES for st in [lane_state.get(lid)] if st]
    fresh_states = [st for st in chain_states if _evidence_is_fresh(st, now)]
    stale_any = len(fresh_states) != len(chain_states)
    eligible_configured = sum(1 for st in fresh_states if _lane_is_subscription(st))
    eligible_open = sum(1 for st in fresh_states if _mode_is_usable_eligible(st))
    reason = REFUSAL_ELIGIBLE_EXHAUSTED if eligible_configured > 0 else REFUSAL_NO_ELIGIBLE_LANE
    error_type = (
        "no_eligible_lane" if reason == REFUSAL_NO_ELIGIBLE_LANE else "eligible_lanes_exhausted"
    )
    error_payload: dict[str, object] = {
        "type": error_type,
        "message": f"no lane satisfies credential_mode={required_mode} for role={role}",
        "eligible_configured": eligible_configured,
        "eligible_open": eligible_open,
    }
    if stale_any:
        # Advisory: the reason may understate because evidence aged out; the
        # operator should check the lane health poll, not just wait/edit config.
        error_payload["credential_evidence_stale"] = True
    resets = [
        st.credential_reset_at
        for st in fresh_states
        if _lane_is_subscription(st) and st.credential_reset_at is not None
    ]
    if resets:
        reset = min(resets)
        if reset > now:
            error_payload["reset_hint_epoch"] = reset
    body = {"type": "error", "error": error_payload}
    return web.json_response(
        body,
        status=403,
        headers={
            CREDENTIAL_MODE_HEADER: CREDENTIAL_MODE_UNKNOWN,
            "x-anthropic-throttle-refusal": reason,
        },
    )


async def _read_lane_health(session: aiohttp.ClientSession, lane: Lane, now: float) -> dict | None:
    """Fetch + parse one lane health body, bounded and fail-closed.

    Returns the parsed dict on success (after the codex ``/healthz`` shape
    normalization); on any failure writes the closed verdict into
    ``lane_state[lane.id]`` and returns None. Shared by the background poll and
    the per-request constrained probe so both see one fetch/parse path.
    """
    try:
        async with session.get(
            lane.health_url, timeout=aiohttp.ClientTimeout(total=LANE_HEALTH_TIMEOUT_S)
        ) as resp:
            if resp.status != 200:
                lane_state[lane.id] = LaneState(False, now, f"health-{resp.status}")
                return None
            if resp.content_length is not None and resp.content_length > LANE_HEALTH_MAX_BYTES:
                lane_state[lane.id] = LaneState(False, now, "health-oversized")
                return None
            body = await resp.json(content_type=None)
            if not isinstance(body, dict):
                lane_state[lane.id] = LaneState(False, now, "health-invalid")
                return None
            # CCP sidecar (codex lane) reports {"ok": bool} instead of the
            # throttle-proxy upstream_egress_ok shape (health-404 finding,
            # 06/08) — normalize at the boundary so lane_usable's shared
            # verdict logic sees one schema. Scoped to the codex lane (codex
            # review MINOR): a non-codex lane with {"ok": ...} must NOT flip
            # to healthy.
            if lane.id == "codex" and "upstream_egress_ok" not in body and "ok" in body:
                body = {**body, "upstream_egress_ok": body.get("ok") is True}
            return body
    except aiohttp.ClientError:
        lane_state[lane.id] = LaneState(False, now, "unreachable")
        return None
    except TimeoutError:
        lane_state[lane.id] = LaneState(False, now, "health-timeout")
        return None
    except (TypeError, ValueError):
        lane_state[lane.id] = LaneState(False, now, "health-invalid")
        return None


def _classify_state(lane: Lane, body: dict, now: float) -> tuple[bool, str, str, str, bool]:
    """Availability + CLASS + CAPACITY verdict for one fresh health body."""
    open_, detail = lane_usable(body, now, proxy_owns_key=lane.proxy_owns_key)
    mode, reason = _classify_lane_class(lane, body)
    return open_, detail, mode, reason, _classify_lane_capacity(body, now)


async def _probe_lane_health(session: aiohttp.ClientSession, lane: Lane) -> None:
    """Per-request fresh probe of a constrained candidate (ADR-6a §1.3)."""
    now = time.time()
    body = await _read_lane_health(session, lane, now)
    if body is None:
        return
    open_, detail, mode, reason, capacity_ok = _classify_state(lane, body, now)
    lane_state[lane.id] = LaneState(
        open_,
        now,
        detail,
        credential_mode=mode,
        credential_mode_reason=reason,
        credential_capacity_ok=capacity_ok,
    )


async def _fresh_probe_candidate(
    session: aiohttp.ClientSession, lane_id: str, lane: Lane | None
) -> None:
    """Re-probe a constrained candidate so E3 is request-fresh (ADR-6a §1.3)."""
    if lane is not None and lane_id in LANES:
        await _probe_lane_health(session, lane)


async def _forward(request: web.Request) -> web.StreamResponse:
    """Forward a request to the selected lane, **spilling to the next lane in the
    role chain on a saturation 503** (the coordinator's ask).

    On ``POST /v1/messages`` the full body is buffered (when it fits) so it's
    re-sendable. If the chosen lane returns a queue-wait-timeout 503 (stamped
    ``x-anthropic-throttle-queue-timeout: 1`` by the sibling proxy lane), the
    ingress marks that lane saturated (short cooldown) and retries the NEXT lane
    in the role's chain — instead of passing the 503 through. Non-spillable
    bodies (too large to buffer, or non-messages) get one attempt. Generate with
    overflow disabled never spills past Anthropic (invariant 6).

    ADR-6a r1 (K3): a request with ``x-anthropic-throttle-require-credential-mode:
    subscription`` is restricted to lanes whose fresh health proves CLASS
    (E1∧E2∧E4) ∧ CAPACITY (E3); refusal is a pre-egress 403 policy verdict,
    never a capacity 503.
    """
    required_mode = _require_credential_mode(request)
    if isinstance(required_mode, web.Response):
        return required_mode

    session: aiohttp.ClientSession = request.app[_SESSION_KEY]
    timeout = aiohttp.ClientTimeout(total=FORWARD_TIMEOUT_S or None)

    is_messages = request.method == "POST" and request.path == "/v1/messages"
    role = "generate"
    sess_key: str | None = None
    prefix = b""
    buffered_rest = b""
    full_body: bytes | None = None  # re-sendable buffered body; None = stream once
    if is_messages:
        prefix, prefix_complete = await _read_bounded(request.content, ROLE_BODY_READ_LIMIT)
        if prefix_complete:
            full_body = prefix
        else:
            buffered_rest, rest_complete = await _read_bounded(
                request.content, REMAP_BODY_MAX_BYTES
            )
            if rest_complete:
                full_body = prefix + buffered_rest
            # else: too large to buffer → full_body stays None (streamed, 1 attempt)
        header_role = routing.role_from_header(request.headers.get(ROLE_OVERRIDE_HEADER))
        role = header_role or infer_role_from_body(prefix)
        # Agentic safety floor: a request with `tools` needs a tools-capable
        # lane. Kimi aborts multi-turn tool-use streaming; GLM has path+key
        # blockers. Force a tools-capable role regardless of model tier or
        # header hint — a consumer must not overflow agentic to a lane that
        # can't handle it (nix w1W:p4 pre-flip gate finding).
        #
        # This one reads the BUFFERED body, not `prefix`. A prefix cut at
        # ROLE_BODY_READ_LIMIT is invalid JSON, and `body_has_tools` answers
        # False on a parse failure — i.e. it fails OPEN, deleting the floor for
        # exactly the requests that need it (a real claude-code turn ships
        # ~100 KiB of tool schemas, so `role-hint: bulk` routed it to Kimi and
        # the turn hung). The neighbours above fail SAFE on the same truncation
        # (role → "generate", session key → None), so they stay on `prefix`:
        # widening them would migrate live non-agentic traffic between lanes,
        # which is a routing decision, not this bug. A body too large to buffer
        # at all can't be inspected, so it fails CLOSED to the capable lane.
        #
        # #182: a SMALL agentic turn (short max_tokens, small body) becomes
        # "code" instead of "generate" — Codex is proven tools-capable (real
        # Codex is agentic by design), so the floor's job (route only to a
        # lane that can handle tools) is satisfied by "code" too. Everything
        # else agentic keeps the existing "generate" floor unchanged.
        if full_body is None:
            role = "generate"
        elif body_has_tools(full_body):
            # One REJECTION evaluation, used for both the decision and its
            # explanation, so the metric can never describe a choice the router
            # didn't make. (The body is still parsed twice overall — once by
            # body_has_tools above — exactly as before this change.)
            reject = code_role_rejection_reason(full_body)
            role = "generate" if reject else "code"
            if reject:
                M_CODE_ROLE_REJECTED.labels(reason=reject).inc()
        sess_key = session_key_from_body(prefix)

    spillable = is_messages and full_body is not None
    tried: set[str] = set()
    used_pin = False
    generate_retries = 0

    while True:
        # Lane selection: session-sticky on the first pick (cache economics),
        # else walk the role's chain. S5 guard: a generate pin to a non-Anthropic
        # lane is only honored while overflow is on (don't silently downgrade).
        # ADR-6a: a constrained request re-probes the candidate lane's health at
        # selection time (per-request attribution) and only accepts a lane whose
        # fresh state is subscription-eligible and open.
        lane_id: str | None = None
        if not used_pin and sess_key is not None and required_mode is None:
            pinned = _session_lane.get(sess_key)
            pin_open = (
                pinned is not None
                and pinned not in tried
                and (lane_state.get(pinned) or LaneState(False, 0)).open
            )
            if (
                pin_open
                and role == "generate"
                and pinned != "anthropic"
                and not routing.GENERATE_OVERFLOW_ENABLED
            ):
                pin_open = False
            if pin_open:
                lane_id = pinned
            used_pin = True
        if lane_id is None:
            lane_id = select_lane(role, lane_state, overflow=routing.GENERATE_OVERFLOW_ENABLED)
            if lane_id is not None and lane_id in tried:
                lane_id = None
            elif lane_id is not None and required_mode is None and sess_key is not None:
                _session_lane[sess_key] = lane_id
        # ADR-6a constrained: re-probe the candidate, then re-check its class.
        if (
            lane_id is not None
            and required_mode is not None
            and lane_id not in tried
            and (lane := LANES.get(lane_id)) is not None
        ):
            await _probe_lane_health(session, lane)
            if not _mode_is_usable_eligible(lane_state.get(lane_id)):
                tried.add(lane_id)
                lane_id = None
                continue  # re-select down the chain; never spill to ineligible

        if lane_id is None or lane_id in tried:
            if required_mode is not None:
                return _policy_refusal(role, required_mode)
            if role == "generate" and not routing.GENERATE_OVERFLOW_ENABLED:
                return web.json_response(
                    {
                        "error": "ingress-generate-held",
                        "reason": "anthropic-capped-overflow-disabled",
                    },
                    status=503,
                )
            return web.json_response(
                {"error": "ingress-all-lanes-capped", "role": role}, status=503
            )
        lane = LANES.get(lane_id)
        if lane is None:
            return web.json_response(
                {"error": "ingress-lane-not-configured", "lane": lane_id}, status=503
            )
        target = f"{lane.url}{request.path_qs}"
        M_ROUTE_DECISIONS.labels(role=role, lane=lane_id).inc()

        # Build the per-lane body from the buffered full_body (re-sendable) or
        # the one-shot stream (large body / non-messages).
        body_data: bytes | AsyncIterator[bytes]
        if is_messages and full_body is not None:
            target_model = lane.models.get(role)
            body_data = remap_body_model(full_body, target_model) if target_model else full_body
        elif is_messages:
            body_data = _chain_stream(request.content, prefix, buffered_rest)
        else:
            body_data = request.content

        upstream: aiohttp.ClientResponse | None = None
        try:
            upstream = await session.request(
                request.method,
                target,
                headers=_forward_headers(request),
                data=body_data,
                timeout=timeout,
                allow_redirects=False,
                auto_decompress=False,
            )
        except aiohttp.ClientError:
            if required_mode is not None:
                tried.add(lane_id)
                continue
            return web.json_response({"error": "ingress-upstream-unreachable"}, status=503)
        except TimeoutError:
            if required_mode is not None:
                tried.add(lane_id)
                continue
            return web.json_response({"error": "ingress-upstream-timeout"}, status=504)

        assert upstream is not None
        # Retryable: saturation-503 (queue full) OR a 429 on a role in
        # _SPILL_ON_429_ROLES. For generate, a 429 that leaked through :8765's
        # own pushback retries — the ingress retries internally (queue-and-wait)
        # so claude-code never sees it, matching direct :8765 behavior where the
        # SDK retries on 429. The nix w1W:p4 flip-gate: without this, a 429 from
        # :8765 reaches claude-code → 60s rate_limit retry → abort. With this,
        # the ingress absorbs the 429 + retries → succeeds on the next attempt.
        #
        # #182/#184: "code" (the Codex lane) is the SAME shape — the ChatGPT
        # usage meter answers a plain 429 "Rate limited" with no stamped
        # saturation header and no Retry-After the ingress can trust, so it must
        # be treated identically: spill to the next lane in the "code" chain
        # (anthropic, then deepseek) rather than streaming the raw 429 to the
        # client. Unlike generate, "code" has no same-lane queue-and-wait
        # fallback below — Codex's meter is account-level saturation (can stay
        # 429 for a while), not a queue that drains in seconds, so spilling to a
        # capable sibling lane is strictly better than retrying the same one.
        is_saturation_503 = (
            upstream.status == 503 and upstream.headers.get(QUEUE_TIMEOUT_HEADER, "").strip() == "1"
        )
        is_retryable = is_saturation_503 or (upstream.status == 429 and role in _SPILL_ON_429_ROLES)
        if is_retryable and spillable:
            upstream.release()
            upstream = None
            tried.add(lane_id)
            if required_mode is None or is_saturation_503:
                _set_lane_state(lane_id, open_=False, detail="saturated")
            # Is there a NEXT lane to spill to (bulk/judge overflow)?
            next_lane = select_lane(role, lane_state, overflow=routing.GENERATE_OVERFLOW_ENABLED)
            if (
                next_lane is not None
                and next_lane not in tried
                and (required_mode is None or _mode_is_usable_eligible(lane_state.get(next_lane)))
            ):
                continue  # spill to the next lane in the chain
            # No overflow lane. For generate, RETRY the same lane
            # (queue-and-wait) instead of 503-aborting — matching direct
            # :8765 behavior where claude-code's SDK retries on 503. The
            # ingress retries internally so the client never sees the abort
            # (the nix w1W:p4 flip-gate requirement).
            if (
                role == "generate"
                and generate_retries < GENERATE_QUEUE_RETRIES
                and (required_mode is None or is_saturation_503)
            ):
                generate_retries += 1
                # Un-mark the lane (it may have drained during the retry delay).
                _set_lane_state(lane_id, open_=True, detail="generate-retry")
                tried.discard(lane_id)
                await asyncio.sleep(GENERATE_QUEUE_RETRY_DELAY_S)
                continue  # re-select + retry the same lane
            # Retries exhausted (or non-generate with no overflow) → 503 / 403.
            if required_mode is not None:
                return _policy_refusal(role, required_mode)
            if role == "generate" and not routing.GENERATE_OVERFLOW_ENABLED:
                return web.json_response(
                    {
                        "error": "ingress-generate-held",
                        "reason": "queue-saturated-after-retries",
                    },
                    status=503,
                )
            return web.json_response(
                {"error": "ingress-all-lanes-capped", "role": role}, status=503
            )
        # Not a saturation-503 (or not spillable) → stream the response through.
        try:
            out_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in _HOP_BY_HOP
                and k.lower() not in _RESERVED_CREDENTIAL_RESPONSE_HEADERS
            }
            resp = web.StreamResponse(status=upstream.status, headers=out_headers)
            resp.headers[MARKER_HEADER] = "1"
            resp.headers[ROLE_HEADER] = role
            resp.headers[LANE_HEADER] = lane_id
            # r1/C3: stamp ONLY responses to requests carrying the requirement
            # header. Constrained-only means the enum collapses on the wire to
            # ``subscription`` on a served 2xx (the lane passed CLASS∧CAPACITY
            # fresh at selection) and ``unknown`` on the 403 refusal — the
            # full four-value vocabulary is normative for per-lane health only.
            if required_mode is not None and 200 <= upstream.status < 300:
                resp.headers[CREDENTIAL_MODE_HEADER] = CREDENTIAL_MODE_SUBSCRIPTION
            await resp.prepare(request)
            async for chunk in upstream.content.iter_any():
                if not chunk:
                    continue
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        finally:
            upstream.release()


async def _root_probe(_request: web.Request) -> web.Response:
    """Local 200 for ``GET /`` / ``HEAD /`` infra probes (PR #29 invariant).

    A load balancer / curl smoke test must not consume a lane slot.
    """
    return web.Response(status=200, text="anthropic-throttle-ingress\n")


async def _health(_request: web.Request) -> web.Response:
    """Fast (<50ms, invariant 4) ingress health. No upstream I/O on the path."""
    return web.json_response(
        {
            "status": "ok",
            "ingress": True,
            "default_lane": DEFAULT_LANE_URL,
            "host": INGRESS_HOST,
            "port": INGRESS_PORT,
            "served": _served,
            "uptime_s": round(time.time() - _start_time, 1),
            # ADR-6a §2.2 capability declaration. ``credential_mode: true`` + a
            # known contract tells a consumer this proxy enforces+attests;
            # absence (old proxy) must be treated as unsupported. r1/C6:
            # publish allowlist count + digest, never the member list.
            "enforcement": {
                "credential_mode": True,
                "contract": ENFORCEMENT_CONTRACT,
                "subscription_upstreams_count": len(_SUBSCRIPTION_UPSTREAMS),
                "subscription_upstreams_digest": _allowlist_digest(),
            },
            # S3: the cached per-lane gauge verdicts so fleet state is visible
            # in one place (read-only snapshot of the in-memory cache, no I/O).
            "lanes": {
                lid: {
                    "open": st.open,
                    "detail": st.detail,
                    "checked_ago_s": round(time.time() - st.checked_at, 1),
                    "credential_mode": st.credential_mode,
                    **(
                        {"credential_mode_reason": st.credential_mode_reason}
                        if st.credential_mode == CREDENTIAL_MODE_UNKNOWN
                        else {}
                    ),
                }
                for lid, st in list(lane_state.items())
            },
        }
    )


async def _metrics(_request: web.Request) -> web.Response:
    """S6: Prometheus scrape of the ingress's process-local registry."""
    body = generate_latest(REGISTRY)
    return web.Response(body=body, content_type="text/plain; version=0.0.4")


@web.middleware
async def _count_served(request: web.Request, handler):
    """Count served requests for health/observability (S6 surfaces this)."""
    global _served
    resp = await handler(request)
    # Skip the control plane AND the root probe so health/metrics/`/` infra
    # probes don't inflate the served counter (mirrors the per-lane proxy
    # convention; PR #29 treats `/` as an infra probe, not served work).
    if request.path not in {"/", "/__throttle/health", "/metrics"}:
        _served += 1
    return resp


async def _poll_one_lane(session: aiohttp.ClientSession, lane: Lane) -> None:
    """One health probe of one lane; updates ``lane_state`` in place."""
    now = time.time()
    body = await _read_lane_health(session, lane, now)
    if body is None:
        return
    open_, detail, mode, reason, capacity_ok = _classify_state(lane, body, now)
    # Optional refusal hint (ADR-6a §3): earliest future blocker across
    # bearers, only when the lane is genuinely budget-blocked.
    reset_at = None
    if not open_ and detail == "no-usable-bearer":
        resets = [
            cand
            for bearer in (body.get("bearers") or {}).values()
            if isinstance(bearer, dict)
            for cand in _bearer_reset_candidates(bearer, now)
        ]
        if resets:
            reset_at = min(resets)
    lane_state[lane.id] = LaneState(
        open_,
        now,
        detail,
        credential_mode=mode,
        credential_mode_reason=reason,
        credential_capacity_ok=capacity_ok,
        credential_reset_at=reset_at,
    )
    # S4: when a lane goes closed, evict sessions pinned to it so the next
    # request re-selects down the chain (stickiness must not pin to a dead lane).
    if not open_:
        _evict_sessions_for_closed_lanes({lane.id})


def _evict_sessions_for_closed_lanes(closed_ids: set[str]) -> int:
    """Drop every session pinned to a now-closed lane. Returns the count evicted."""
    n = 0
    for key, pinned in list(_session_lane.items()):
        if pinned in closed_ids:
            _session_lane.pop(key, None)
            n += 1
    return n


async def _poll_lanes_once(session: aiohttp.ClientSession) -> None:
    """Probe every configured lane concurrently so one slow lane can't stall the rest."""
    await asyncio.gather(*(_poll_one_lane(session, lane) for lane in LANES.values()))


async def _lane_health_context(app: web.Application):
    """S3: background lane-health poll. Does one synchronous poll at startup so
    ``lane_state`` is populated before the first forward (no cold-start 503
    storm), then re-polls every ``LANE_HEALTH_INTERVAL_S``.

    Disabled (no initial poll, no loop) when ``LANE_HEALTH_INTERVAL_S <= 0`` —
    lets an operator pin ``lane_state`` manually and keeps the test-suite
    deterministic (it sets ``lane_state`` directly instead of racing the loop).
    """
    if LANE_HEALTH_INTERVAL_S <= 0:
        yield
        return
    session = app[_SESSION_KEY]
    await _poll_lanes_once(session)  # initial poll before serving
    task = asyncio.create_task(_lane_health_loop(session))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _lane_health_loop(session: aiohttp.ClientSession) -> None:
    """Re-probe lane health on a fixed cadence until shutdown."""
    while True:
        await asyncio.sleep(LANE_HEALTH_INTERVAL_S)
        # Never let the poll loop die — health is load-bearing and a transient
        # error in one cycle must not stop future probes.
        with contextlib.suppress(Exception):
            await _poll_lanes_once(session)


async def _session_context(app: web.Application):
    """Lifecycle-managed ClientSession: one pool for all forwards, cleaned up."""
    session = aiohttp.ClientSession(headers={"User-Agent": "anthropic-throttle-ingress/0.1"})
    app[_SESSION_KEY] = session
    try:
        yield
    finally:
        await session.close()


def _warn_roles_without_a_lane() -> list[str]:
    """Roles whose whole chain has been retired — they can only HOLD.

    Retiring the last non-Anthropic lane silently turns `bulk` into a permanent
    503, because bulk deliberately excludes Anthropic (invariant 2: bulk must
    not draw the Opus meter). That is a policy decision, not an accident, so it
    must be visible at boot rather than discovered by a stalled subagent.
    """
    orphaned = [
        role
        for role, chain in routing.ROLE_CHAINS.items()
        if not any(lane_id in LANES for lane_id in chain)
    ]
    for role in orphaned:
        print(
            f"[ingress] role={role} has NO configured lane "
            f"(chain={routing.ROLE_CHAINS[role]}) — every {role} request will HOLD",
            flush=True,
        )
    return orphaned


def build_app() -> web.Application:
    """Wire the ingress aiohttp app (route table + lifecycle hooks)."""
    _warn_roles_without_a_lane()
    app = web.Application(client_max_size=128 * 1024 * 1024, middlewares=[_count_served])
    app.cleanup_ctx.append(_session_context)
    app.cleanup_ctx.append(_lane_health_context)  # S3: depends on the session existing
    app.router.add_get("/", _root_probe)
    app.router.add_get("/__throttle/health", _health)
    app.router.add_get("/metrics", _metrics)
    app.router.add_route("*", "/{path:.*}", _forward)
    return app


def main() -> None:
    """Boot the unified ingress on ``INGRESS_HOST:INGRESS_PORT``."""
    app = build_app()
    web.run_app(
        app,
        host=INGRESS_HOST,
        port=INGRESS_PORT,
        print=None,
        shutdown_timeout=float(os.environ.get("INGRESS_SHUTDOWN_TIMEOUT_S", "85")),
    )


if __name__ == "__main__":
    main()
