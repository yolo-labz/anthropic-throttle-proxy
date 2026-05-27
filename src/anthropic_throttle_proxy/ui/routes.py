"""HTMX dashboard endpoints.

Routes:
    GET  /ui              — full page (Catppuccin Mocha, one HTMX script).
    GET  /ui/stats        — `<table>` partial; hx-trigger fires every 2 s.
    GET  /ui/config       — config-editor form partial (one row per editable knob).
    POST /ui/config       — set one knob's runtime override (validates + persists).
    POST /ui/config/reset — drop one knob's runtime override, restore env default.
    GET  /ui/static/...   — CSS + favicon.
    POST /ui/advisor      — optional GROQ call (gated by ADVISOR_ENABLED).

The hot path proxy is NOT routed through this module. Failure to render the
UI must not break /v1/messages.
"""

from __future__ import annotations

import os
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web

from .. import config as _config

# Lazy import: keep the proxy hot path free of UI deps.
from .. import proxy as _proxy

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"

# Utilization at/above this fraction of a unified window counts as "pacing"
# even before Anthropic flips the window to ``rejected``.
_PACING_UTIL = 0.80


def _compute_status(bearers: list[dict], queue_mode: str) -> dict[str, object]:
    """Derive one fleet-wide verdict from the live snapshot (drives the status strip).

    Worst-wins across bearers: ``throttled`` > ``pacing`` > ``healthy``. The
    ``binding`` line names the most-constrained bearer so the operator sees the
    single thing holding the fleet back without scanning the table.
    """
    if not bearers:
        return {
            "level": "idle",
            "verdict": "IDLE",
            "detail": "no bearers yet — point a client at this proxy to start.",
        }

    n = len(bearers)
    throttled: list[str] = []
    pacing: list[str] = []
    binding: tuple[float, str, object] | None = None  # (util_5h, bearer_id, retry_after)

    for b in bearers:
        unified = b.get("unified") or {}
        util_5h = unified.get("util_5h")
        status_5h = unified.get("status") or unified.get("status_5h")
        retry_after = (b.get("last_ratelimit") or {}).get("retry-after")
        lim = b.get("limiter") or {}
        live, hard = lim.get("max_concurrent"), lim.get("hard_max")
        shrunk = live is not None and hard is not None and live < hard

        if status_5h == "rejected" or retry_after:
            throttled.append(b["bearer_id"])
        elif shrunk or (util_5h is not None and util_5h >= _PACING_UTIL) or b.get("queued", 0) > 0:
            pacing.append(b["bearer_id"])

        if util_5h is not None and (binding is None or util_5h > binding[0]):
            binding = (util_5h, b["bearer_id"], retry_after)

    plural = "s" if n != 1 else ""
    if throttled:
        level, verdict = "throttled", "THROTTLED"
        detail = f"{len(throttled)} of {n} bearer{plural} throttled"
    elif pacing:
        level, verdict = "pacing", "PACING"
        detail = f"{len(pacing)} of {n} bearer{plural} pacing"
    else:
        level, verdict = "healthy", "HEALTHY"
        detail = f"all {n} bearer{plural} clear"

    if binding is not None:
        detail += f" · binding: 5h window {round(binding[0] * 100)}% on {binding[1]}"
        if binding[2]:
            detail += f" · retry-after {binding[2]}"
    if queue_mode == "off":
        detail += " · queue off (passthrough)"
    return {"level": level, "verdict": verdict, "detail": detail}


def _collect_view() -> dict[str, object]:
    """Snapshot the proxy's globals into a JSON-safe view for the template."""
    cs = _proxy.state["central_status"]
    bearers = []
    for bid, bstate in _proxy.bearer_state.items():
        lim = _proxy.bearer_limiters.get(bid)
        bearers.append(
            {
                "bearer_id": bid,
                "inflight": bstate.get("inflight", 0),
                "queued": bstate.get("queued", 0),
                "served": bstate.get("served", 0),
                "last_ratelimit": bstate.get("last_ratelimit"),
                "unified": bstate.get("unified"),
                "limiter": lim.snapshot() if lim is not None else None,
            }
        )
    return {
        "status": _compute_status(bearers, _proxy.QUEUE_MODE),
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
        "last_advisor": _proxy.state.get("last_advisor"),
    }


async def index(request: web.Request) -> web.Response:
    """GET /ui — render the full HTMX dashboard page."""
    return aiohttp_jinja2.render_template("dashboard.html", request, _collect_view())


async def stats_partial(request: web.Request) -> web.Response:
    """GET /ui/stats — render the live stats ``<table>`` partial (hx-polled)."""
    return aiohttp_jinja2.render_template("partials/stats.html", request, _collect_view())


async def advisor(request: web.Request) -> web.Response:
    """POST /ui/advisor — ask GROQ to recommend knob tweaks.

    Always returns 200 with a rendered HTML partial so HTMX swaps the
    response into ``#advisor-out`` regardless of error state. Returning
    non-2xx would leave the dashboard's response area silently empty,
    which Pedro reported on 27/05/2026 ("groq integration that does not
    work" — the integration *did* work, but errors landed off-screen).
    """
    if os.environ.get("ADVISOR_ENABLED", "false").lower() != "true":
        return aiohttp_jinja2.render_template(
            "partials/advisor.html",
            request,
            {
                "recommendation": None,
                "snapshot": None,
                "error": (
                    "Advisor is disabled. Set `ADVISOR_ENABLED=true` and "
                    "`GROQ_API_KEY` (proxy reads them from the EnvironmentFile "
                    "at ~/.local/state/anthropic-throttle-proxy/groq.env), "
                    "then restart the service."
                ),
            },
        )
    # Lazy import — keeps the advisor (and its HTTP client) off the hot path.
    from .advisor_impl import recommend

    snapshot = _collect_view()
    try:
        recommendation = await recommend(snapshot)
    except Exception as exc:
        return aiohttp_jinja2.render_template(
            "partials/advisor.html",
            request,
            {
                "recommendation": None,
                "snapshot": snapshot,
                "error": f"Advisor call failed: {exc!s}",
            },
        )
    return aiohttp_jinja2.render_template(
        "partials/advisor.html",
        request,
        {"recommendation": recommendation, "snapshot": snapshot, "error": None},
    )


async def config_form(request: web.Request) -> web.Response:
    """GET /ui/config — render the editable-knobs form partial."""
    return aiohttp_jinja2.render_template(
        "partials/config.html",
        request,
        {"knobs": _config.knob_snapshot(), "message": None, "error": None},
    )


async def config_set(request: web.Request) -> web.Response:
    """POST /ui/config — apply one knob's runtime override (form data: ``key``, ``value``).

    Returns the re-rendered config partial with a status message. HTMX swaps
    the section in place; no page reload.
    """
    form = await request.post()
    key = str(form.get("key", "")).strip()
    raw_value = form.get("value", "")
    message: str | None = None
    error: str | None = None
    if not key:
        error = "missing 'key' field"
    else:
        try:
            value = _config.set_override(key, raw_value)
            message = f"{key} → {value}"
        except KeyError as exc:
            error = f"unknown knob: {exc!s}"
        except (ValueError, TypeError) as exc:
            error = f"invalid value: {exc!s}"
    return aiohttp_jinja2.render_template(
        "partials/config.html",
        request,
        {"knobs": _config.knob_snapshot(), "message": message, "error": error},
    )


async def config_reset(request: web.Request) -> web.Response:
    """POST /ui/config/reset — drop one knob's runtime override.

    Restores the env-default value for the named knob and removes the entry
    from the persisted overrides file. Returns the re-rendered partial.
    """
    form = await request.post()
    key = str(form.get("key", "")).strip()
    message: str | None = None
    error: str | None = None
    try:
        restored = _config.reset_override(key)
        message = f"{key} reset → {restored}"
    except KeyError as exc:
        error = f"unknown knob: {exc!s}"
    return aiohttp_jinja2.render_template(
        "partials/config.html",
        request,
        {"knobs": _config.knob_snapshot(), "message": message, "error": error},
    )


def attach_ui(app: web.Application) -> None:
    """Wire jinja2 + the /ui routes onto an existing aiohttp app."""
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(_TEMPLATES)))
    app.router.add_get("/ui", index)
    app.router.add_get("/ui/stats", stats_partial)
    app.router.add_get("/ui/config", config_form)
    app.router.add_post("/ui/config", config_set)
    app.router.add_post("/ui/config/reset", config_reset)
    app.router.add_post("/ui/advisor", advisor)
    app.router.add_static("/ui/static/", _STATIC, follow_symlinks=False)
