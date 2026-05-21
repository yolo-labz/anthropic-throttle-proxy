"""HTMX dashboard endpoints.

Routes:
    GET  /ui              — full page (Catppuccin Mocha, one HTMX script).
    GET  /ui/stats        — `<table>` partial; hx-trigger fires every 2 s.
    GET  /ui/static/...   — CSS + favicon.
    POST /ui/advisor      — optional Haiku call (gated by ADVISOR_ENABLED).

The hot path proxy is NOT routed through this module. Failure to render the
UI must not break /v1/messages.
"""

from __future__ import annotations

import os
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web

# Lazy import: keep the proxy hot path free of UI deps.
from .. import proxy as _proxy

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"


def _collect_view() -> dict:
    """Snapshot the proxy's globals into a JSON-safe view for the template."""
    cs = _proxy.state["central_status"]
    bearers = []
    for bid, bstate in _proxy.bearer_state.items():
        lim = _proxy.bearer_limiters.get(bid)
        bearers.append({
            "bearer_id": bid,
            "inflight": bstate.get("inflight", 0),
            "queued": bstate.get("queued", 0),
            "served": bstate.get("served", 0),
            "limiter": lim.snapshot() if lim is not None else None,
        })
    return {
        "inflight": _proxy.state["inflight"],
        "queued": _proxy.state["queued"],
        "served": _proxy.state["served"],
        "disconnects": _proxy.state["client_disconnects"],
        "retries": _proxy.state["upstream_retries"],
        "max_concurrent": _proxy.MAX_CONCURRENT,
        "queue_mode": _proxy.QUEUE_MODE,
        "min_dispatch_gap_ms": int(_proxy.MIN_DISPATCH_GAP_S * 1000),
        "upstream": _proxy.UPSTREAM,
        "central_url": _proxy.CENTRAL_URL or "(direct)",
        "central_status": cs,
        "bearers": bearers,
        "advisor_enabled": os.environ.get("ADVISOR_ENABLED", "false").lower() == "true",
    }


async def index(request: web.Request) -> web.Response:
    return aiohttp_jinja2.render_template("dashboard.html", request, _collect_view())


async def stats_partial(request: web.Request) -> web.Response:
    return aiohttp_jinja2.render_template("partials/stats.html", request, _collect_view())


async def advisor(request: web.Request) -> web.Response:
    """POST /ui/advisor — ask Anthropic Haiku to recommend knob tweaks."""
    if os.environ.get("ADVISOR_ENABLED", "false").lower() != "true":
        return web.Response(
            status=503,
            text="ADVISOR_ENABLED=false. Set the env var + ANTHROPIC_API_KEY to enable.",
        )
    # Lazy import — keeps the SDK off the hot path.
    from .advisor_impl import recommend

    snapshot = _collect_view()
    try:
        recommendation = await recommend(snapshot)
    except Exception as exc:  # noqa: BLE001 — surface to the user
        return web.Response(status=500, text=f"advisor error: {exc!s}")
    return aiohttp_jinja2.render_template(
        "partials/advisor.html",
        request,
        {"recommendation": recommendation, "snapshot": snapshot},
    )


def attach_ui(app: web.Application) -> None:
    """Wire jinja2 + the /ui routes onto an existing aiohttp app."""
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(_TEMPLATES)))
    app.router.add_get("/ui", index)
    app.router.add_get("/ui/stats", stats_partial)
    app.router.add_post("/ui/advisor", advisor)
    app.router.add_static("/ui/static/", _STATIC, follow_symlinks=False)
