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

from . import routing
from .routing import (
    Lane,
    LaneState,
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

# S2: the inferred role (generate/judge/bulk) stamped on served responses for
# observability. S6 surfaces per-(role→lane) decision counts.
ROLE_HEADER: Final[str] = "x-anthropic-throttle-role"

# S2: the lane id the ingress routed to, stamped on served responses.
LANE_HEADER: Final[str] = "x-anthropic-throttle-lane"

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
    is True iff the whole body fit (EOF reached). ``StreamReader.read(limit)``
    returns up to ``limit`` bytes (not a full chunk like ``readany``), so a body
    larger than ``limit`` yields a bounded prefix and the role parse runs on only
    that prefix — the S2 contract (body > limit → default role, no full parse)."""
    data = await stream.read(limit)
    complete = len(data) < limit or stream.at_eof()
    return data, complete


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


def _forward_headers(request: web.Request) -> dict[str, str]:
    """Client headers minus hop-by-hop, ready for the upstream request."""
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}


async def _forward(request: web.Request) -> web.StreamResponse:
    """Forward one request to the selected lane, path-preservingly.

    Path + query string are preserved verbatim, the body is streamed (or buffered
    for ``POST /v1/messages`` so S2 can read the model), and the upstream
    response is streamed back byte-identical. S2 infers the role; **S3 selects
    the lane** by walking the role's chain over the cached gauge verdicts
    (``select_lane``) and forwards to that lane's URL. If every lane for the role
    is capped, the request is held with a 503 (S5 refines the HOLD+flag policy;
    S3's bar is the basic all-capped 503).
    """
    session: aiohttp.ClientSession = request.app[_SESSION_KEY]
    timeout = aiohttp.ClientTimeout(total=FORWARD_TIMEOUT_S or None)

    # S2/S4: on POST /v1/messages, read a bounded prefix to infer the role +
    # session key. Other paths stream unchanged (no model to read).
    is_messages = request.method == "POST" and request.path == "/v1/messages"
    role = "generate"
    sess_key: str | None = None
    prefix = b""
    prefix_complete = True
    if is_messages:
        prefix, prefix_complete = await _read_bounded(request.content, ROLE_BODY_READ_LIMIT)
        role = infer_role_from_body(prefix)
        sess_key = session_key_from_body(prefix)

    # S4 session stickiness: a pinned, still-open lane wins over the chain walk
    # (cache economics — avoid a mid-session uncached turn). Else select_lane.
    # S5 guard: a generate pin to a non-Anthropic lane is only honored while
    # overflow is on — if overflow was toggled off, drop the pin so we don't
    # silently downgrade generate to kimi/glm (invariant 6).
    lane_id: str | None = None
    if sess_key is not None:
        pinned = _session_lane.get(sess_key)
        pin_open = pinned is not None and (lane_state.get(pinned) or LaneState(False, 0)).open
        if (
            pin_open
            and role == "generate"
            and pinned != "anthropic"
            and not routing.GENERATE_OVERFLOW_ENABLED
        ):
            pin_open = False
        if pin_open:
            lane_id = pinned
    if lane_id is None:
        lane_id = select_lane(role, lane_state, overflow=routing.GENERATE_OVERFLOW_ENABLED)
        if lane_id is not None and sess_key is not None:
            _session_lane[sess_key] = lane_id

    # S3/S5: no open lane for the role → HOLD (503). Generate with overflow
    # disabled HOLDs distinctly (invariant 6: don't silently downgrade to
    # kimi-k2.6/GLM served as Opus pre-kimi-k3) so the operator can tell a
    # deliberate generate-hold from a genuine all-capped.
    if lane_id is None:
        if role == "generate" and not routing.GENERATE_OVERFLOW_ENABLED:
            return web.json_response(
                {"error": "ingress-generate-held", "reason": "anthropic-capped-overflow-disabled"},
                status=503,
            )
        return web.json_response({"error": "ingress-all-lanes-capped", "role": role}, status=503)
    lane = LANES.get(lane_id)
    if lane is None:  # defensive: chain named a lane not in the registry
        return web.json_response(
            {"error": "ingress-lane-not-configured", "lane": lane_id}, status=503
        )
    target = f"{lane.url}{request.path_qs}"

    # S4 model-remap: a non-Anthropic lane's upstream expects its own id, so
    # rewrite the body's model field on egress (client keeps its claude-* id).
    # Content-Length is stripped in _forward_headers so aiohttp recomputes it from
    # the (possibly length-changed) bytes actually sent.
    body_data: bytes | AsyncIterator[bytes] = request.content
    if is_messages:
        target_model = lane.models.get(role)
        if target_model:
            if prefix_complete:
                body_data = remap_body_model(prefix, target_model)
            else:
                rest, rest_complete = await _read_bounded(request.content, REMAP_BODY_MAX_BYTES)
                if rest_complete:
                    body_data = remap_body_model(prefix + rest, target_model)
                else:
                    body_data = _chain_stream(request.content, prefix, rest)
        elif prefix_complete:
            body_data = prefix
        else:
            body_data = _chain_stream(request.content, prefix)

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
        # Generic body only — never echo upstream exception text to the client
        # (it can leak internal paths / connection details). Server-side
        # observability lands with S6.
        return web.json_response({"error": "ingress-upstream-unreachable"}, status=503)
    except TimeoutError:
        return web.json_response({"error": "ingress-upstream-timeout"}, status=504)

    assert upstream is not None
    try:
        # Drop hop-by-hop from the upstream response too; keep the rest verbatim
        # so SSE / content-type / rate-limit headers pass through unchanged.
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
        resp = web.StreamResponse(status=upstream.status, headers=out_headers)
        resp.headers[MARKER_HEADER] = "1"
        resp.headers[ROLE_HEADER] = role  # S2 observability (S6 surfaces counts)
        resp.headers[LANE_HEADER] = lane_id  # S3: which lane served the role
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
    open_, detail = lane_usable(body, now)
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


def build_app() -> web.Application:
    """Wire the ingress aiohttp app (route table + lifecycle hooks)."""
    app = web.Application(client_max_size=128 * 1024 * 1024, middlewares=[_count_served])
    app.cleanup_ctx.append(_session_context)
    app.cleanup_ctx.append(_lane_health_context)  # S3: depends on the session existing
    app.router.add_get("/", _root_probe)
    app.router.add_get("/__throttle/health", _health)
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
