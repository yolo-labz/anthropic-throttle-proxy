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
    default_lanes,
    infer_role_from_body,
    is_small_agentic_code,
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
_INGRESS_ONLY_HEADERS: Final[frozenset[str]] = frozenset({ROLE_OVERRIDE_HEADER})


def _forward_headers(request: web.Request) -> dict[str, str]:
    """Client headers minus hop-by-hop + ingress-internal, ready for upstream."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _INGRESS_ONLY_HEADERS
    }


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
    """
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
            role = "code" if is_small_agentic_code(full_body) else "generate"
        sess_key = session_key_from_body(prefix)

    spillable = is_messages and full_body is not None
    tried: set[str] = set()
    used_pin = False
    generate_retries = 0

    while True:
        # Lane selection: session-sticky on the first pick (cache economics),
        # else walk the role's chain. S5 guard: a generate pin to a non-Anthropic
        # lane is only honored while overflow is on (don't silently downgrade).
        lane_id: str | None = None
        if not used_pin and sess_key is not None:
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
            elif lane_id is not None and sess_key is not None:
                _session_lane[sess_key] = lane_id

        if lane_id is None or lane_id in tried:
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
            return web.json_response({"error": "ingress-upstream-unreachable"}, status=503)
        except TimeoutError:
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
            lane_state[lane_id] = LaneState(False, time.time(), "saturated")
            # Is there a NEXT lane to spill to (bulk/judge overflow)?
            next_lane = select_lane(role, lane_state, overflow=routing.GENERATE_OVERFLOW_ENABLED)
            if next_lane is not None and next_lane not in tried:
                continue  # spill to the next lane in the chain
            # No overflow lane. For generate, RETRY the same lane
            # (queue-and-wait) instead of 503-aborting — matching direct
            # :8765 behavior where claude-code's SDK retries on 503. The
            # ingress retries internally so the client never sees the abort
            # (the nix w1W:p4 flip-gate requirement).
            if role == "generate" and generate_retries < GENERATE_QUEUE_RETRIES:
                generate_retries += 1
                # Un-mark the lane (it may have drained during the retry delay).
                lane_state[lane_id] = LaneState(True, time.time(), "generate-retry")
                tried.discard(lane_id)
                await asyncio.sleep(GENERATE_QUEUE_RETRY_DELAY_S)
                continue  # re-select + retry the same lane
            # Retries exhausted (or non-generate with no overflow) → 503.
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
                k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
            }
            resp = web.StreamResponse(status=upstream.status, headers=out_headers)
            resp.headers[MARKER_HEADER] = "1"
            resp.headers[ROLE_HEADER] = role
            resp.headers[LANE_HEADER] = lane_id
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
            # S3: the cached per-lane gauge verdicts so fleet state is visible
            # in one place (read-only snapshot of the in-memory cache, no I/O).
            "lanes": {
                lid: {
                    "open": st.open,
                    "detail": st.detail,
                    "checked_ago_s": round(time.time() - st.checked_at, 1),
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
    try:
        async with session.get(
            lane.health_url, timeout=aiohttp.ClientTimeout(total=LANE_HEALTH_TIMEOUT_S)
        ) as resp:
            if resp.status != 200:
                lane_state[lane.id] = LaneState(False, now, f"health-{resp.status}")
                return
            # Bound the parse (gate MAJOR): reject an oversized health body rather
            # than loading it. content_length is None for chunked; fall through to
            # the bounded read in that case.
            if resp.content_length is not None and resp.content_length > LANE_HEALTH_MAX_BYTES:
                lane_state[lane.id] = LaneState(False, now, "health-oversized")
                return
            body = await resp.json(content_type=None)
    except aiohttp.ClientError:
        lane_state[lane.id] = LaneState(False, now, "unreachable")
        return
    except TimeoutError:
        lane_state[lane.id] = LaneState(False, now, "health-timeout")
        return
    open_, detail = lane_usable(body, now, proxy_owns_key=lane.proxy_owns_key)
    lane_state[lane.id] = LaneState(open_, now, detail)
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
