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

import os
import time
from typing import Final

import aiohttp
from aiohttp import web

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

# Stamp on every served response so a downstream tier / probe can tell an
# ingress-served response from a direct-lane response.
MARKER_HEADER: Final[str] = "x-anthropic-throttle-ingress"

# Hop-by-hop headers (RFC 7230 §6.1) — must not be forwarded verbatim; aiohttp
# also manages Content-Length / Transfer-Encoding on the rebuilt request.
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
    """Forward one request to the default lane, path-preservingly.

    Path + query string are preserved verbatim (``request.path_qs``), the body
    is streamed, and the upstream response is streamed back byte-identical. In
    S1 this is identical to the client pointing at the lane directly; later
    slices swap the target URL + remap the body model without touching this
    shape.
    """
    session: aiohttp.ClientSession = request.app[_SESSION_KEY]
    target = f"{DEFAULT_LANE_URL}{request.path_qs}"
    timeout = aiohttp.ClientTimeout(total=FORWARD_TIMEOUT_S or None)
    upstream: aiohttp.ClientResponse | None = None
    try:
        upstream = await session.request(
            request.method,
            target,
            headers=_forward_headers(request),
            data=request.content,
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
