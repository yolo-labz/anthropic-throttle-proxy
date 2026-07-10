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

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web

from .. import accounts as _accounts
from .. import config as _config
from .. import copilot as _copilot
from .. import fleet as _fleet
from .. import metrics as _metrics

# Lazy import: keep the proxy hot path free of UI deps.
from .. import proxy as _proxy

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"

# Utilization at/above this fraction of a unified window counts as "pacing"
# even before Anthropic flips the window to ``rejected``.
_PACING_UTIL = 0.80

# Partial-template paths, named once so SonarQube python:S1192 (duplicated
# literal) stays clean. f-strings keep the full literal out of the source.
_PARTIALS = "partials"
_TPL_ADVISOR = f"{_PARTIALS}/advisor.html"
_TPL_CONFIG = f"{_PARTIALS}/config.html"


def _bearer_pacing_state(b: dict) -> tuple[str | None, float | None, object]:
    """Classify one bearer for the status strip.

    Returns ``(state, util_5h, retry_after)`` where state is ``"throttled"`` /
    ``"pacing"`` / ``None`` (clear). Worst-wins aggregation is the caller's job.
    """
    unified = b.get("unified") or {}
    util_5h = unified.get("util_5h")
    status_5h = unified.get("status") or unified.get("status_5h")
    retry_after = (b.get("last_ratelimit") or {}).get("retry-after")
    lim = b.get("limiter") or {}
    live, hard = lim.get("max_concurrent"), lim.get("hard_max")
    shrunk = live is not None and hard is not None and live < hard
    over_pacing = util_5h is not None and util_5h >= _PACING_UTIL
    if status_5h == "rejected" or retry_after:
        return "throttled", util_5h, retry_after
    if shrunk or over_pacing or b.get("queued", 0) > 0:
        return "pacing", util_5h, retry_after
    return None, util_5h, retry_after


def _fleet_verdict(n: int, throttled: int, pacing: int) -> tuple[str, str, str]:
    """Worst-wins ``(level, verdict, detail-prefix)`` for ``n`` bearers."""
    plural = "s" if n != 1 else ""
    if throttled:
        return "throttled", "THROTTLED", f"{throttled} of {n} bearer{plural} throttled"
    if pacing:
        return "pacing", "PACING", f"{pacing} of {n} bearer{plural} pacing"
    return "healthy", "HEALTHY", f"all {n} bearer{plural} clear"


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

    throttled: list[str] = []
    pacing: list[str] = []
    binding: tuple[float, str, object] | None = None  # (util_5h, bearer_id, retry_after)
    for b in bearers:
        state, util_5h, retry_after = _bearer_pacing_state(b)
        if state == "throttled":
            throttled.append(b["bearer_id"])
        elif state == "pacing":
            pacing.append(b["bearer_id"])
        if util_5h is not None and (binding is None or util_5h > binding[0]):
            binding = (util_5h, b["bearer_id"], retry_after)

    level, verdict, detail = _fleet_verdict(len(bearers), len(throttled), len(pacing))
    if binding is not None:
        detail += f" · binding: 5h window {round(binding[0] * 100)}% on {binding[1]}"
        if binding[2]:
            detail += f" · retry-after {binding[2]}"
    if queue_mode == "off":
        detail += " · queue off (passthrough)"
    return {"level": level, "verdict": verdict, "detail": detail}


# account label -> last-published scoped model, so a Fable→Sonnet flip drops the
# stale per-model series instead of freezing it forever (Codex MEDIUM). Both this
# and the registry are process-local, so they stay in sync across a restart.
_scoped_model_seen: dict[str, str] = {}


def _publish_account_gauges(
    endpoint: dict[str, dict[str, object]], identity: dict[str, object]
) -> None:
    """Mirror endpoint truth into /metrics so Grafana sees what /ui sees."""
    for label, path in _accounts.parse_spec(_config.ACCOUNT_CRED_PATHS):
        usage = (endpoint.get(path) or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        for window, ukey, rkey in (("5h", "util_5h", "reset_5h"), ("7d", "util_7d", "reset_7d")):
            util, reset = usage.get(ukey), usage.get(rkey)
            if util is not None:
                _metrics.M_ACCOUNT_USAGE.labels(label, window).set(util)
            if reset is not None:
                _metrics.M_ACCOUNT_RESET.labels(label, window).set(reset)
        # Spec 2: weekly per-model (scoped) meter — labeled by the model it
        # currently tracks so a Fable→Sonnet flip is visible per account.
        scoped = usage.get("scoped")
        if isinstance(scoped, dict) and scoped.get("util") is not None and scoped.get("model"):
            model = str(scoped["model"])
            prev = _scoped_model_seen.get(label)
            if prev is not None and prev != model:
                # Model flipped — drop the stale series (prev was published, so
                # the labelset exists; safe to remove without a guard).
                _metrics.M_ACCOUNT_SCOPED.remove(label, prev)
            _scoped_model_seen[label] = model
            _metrics.M_ACCOUNT_SCOPED.labels(label, model).set(scoped["util"])
    suspected = identity.get("suspected") or {}
    if identity["collapsed"]:
        _metrics.M_ACCOUNTS_DISTINCT.set(0)
    elif suspected:
        # Shared email pending live-token verification — unknown, not "distinct".
        _metrics.M_ACCOUNTS_DISTINCT.set(-1)
    elif int(identity["known"]) >= 2:  # type: ignore[call-overload]
        _metrics.M_ACCOUNTS_DISTINCT.set(1)
    else:
        _metrics.M_ACCOUNTS_DISTINCT.set(-1)
    # FR-005: partial collisions (some-but-not-all stores share an account) that
    # the binary distinct gauge above reads as "distinct". duplicates from the
    # richer identity verdict → count of stores tied to a VERIFIED non-unique
    # account; suspected → stores pending live-probe verification.
    duplicates = identity.get("duplicates") or {}
    _metrics.M_ACCOUNT_COLLISIONS.set(sum(len(labels) for labels in duplicates.values()))
    _metrics.M_ACCOUNT_SUSPECTED.set(sum(len(labels) for labels in suspected.values()))


async def _collect_view() -> dict[str, object]:
    """Snapshot the proxy's globals into a JSON-safe view for the template."""
    cs = _proxy.state["central_status"]
    labels = _accounts.bearer_labels()
    bearers = []
    for bid, bstate in _proxy.bearer_state.items():
        lim = _proxy.bearer_limiters.get(bid)
        bearers.append(
            {
                "bearer_id": bid,
                "account": labels.get(bid),
                "inflight": bstate.get("inflight", 0),
                "queued": bstate.get("queued", 0),
                "served": bstate.get("served", 0),
                "last_ratelimit": bstate.get("last_ratelimit"),
                "unified": bstate.get("unified"),
                "limiter": lim.snapshot() if lim is not None else None,
            }
        )
    now = time.time()
    endpoint = await _accounts.refresh_endpoint(now)
    accounts_view = _accounts.account_view(bearers, now, endpoint)
    identity = _accounts.identity_state(accounts_view)
    _publish_account_gauges(endpoint, identity)
    # Fleet + Copilot are concurrent with the account refresh — both are
    # failure-tolerant (a down sibling / 403 org renders as such, never raises).
    # return_exceptions: a future regression in one panel must never blank the
    # other panels or the bearer table — coerce any exception to an empty list
    # (panel hides) rather than a 500.
    fleet_raw, copilot_raw = await asyncio.gather(
        _fleet.refresh(now), _copilot.refresh(now), return_exceptions=True
    )
    fleet_view = fleet_raw if isinstance(fleet_raw, list) else []
    copilot_view = copilot_raw if isinstance(copilot_raw, list) else []
    return {
        "accounts": accounts_view,
        "identity": identity,
        "fleet": fleet_view,
        "copilot": copilot_view,
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


async def index(
    request: web.Request,
) -> web.Response:
    """GET /ui — render the full HTMX dashboard page."""
    return aiohttp_jinja2.render_template("dashboard.html", request, await _collect_view())


async def stats_partial(
    request: web.Request,
) -> web.Response:
    """GET /ui/stats — render the live stats ``<table>`` partial (hx-polled)."""
    return aiohttp_jinja2.render_template("partials/stats.html", request, await _collect_view())


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
            _TPL_ADVISOR,
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

    snapshot = await _collect_view()
    try:
        recommendation = await recommend(snapshot)
    except Exception as exc:
        return aiohttp_jinja2.render_template(
            _TPL_ADVISOR,
            request,
            {
                "recommendation": None,
                "snapshot": snapshot,
                "error": f"Advisor call failed: {exc!s}",
            },
        )
    return aiohttp_jinja2.render_template(
        _TPL_ADVISOR,
        request,
        {"recommendation": recommendation, "snapshot": snapshot, "error": None},
    )


async def config_form(
    request: web.Request,
) -> web.Response:
    """GET /ui/config — render the editable-knobs form partial."""
    return aiohttp_jinja2.render_template(
        _TPL_CONFIG,
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
        _TPL_CONFIG,
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
        _TPL_CONFIG,
        request,
        {"knobs": _config.knob_snapshot(), "message": message, "error": error},
    )


# Background cadence for the account-endpoint refresher: keeps /metrics
# gauges + the email cache warm with NO dashboard viewer. 300s matches the
# polling guidance the usage endpoint tolerates comfortably (its own TTL
# inside refresh_endpoint additionally dedupes against dashboard renders).
_REFRESH_INTERVAL_S = 300.0


async def _account_refresh_loop() -> None:
    """Slow loop publishing account endpoint truth to /metrics."""
    log = logging.getLogger("throttle.ui.accounts")
    while True:
        try:
            now = time.time()
            endpoint = await _accounts.refresh_endpoint(now)
            view = _accounts.account_view([], now, endpoint)
            _publish_account_gauges(endpoint, _accounts.identity_state(view))
        except Exception as exc:  # noqa: BLE001 — a UI nicety must never crash the app
            log.debug("account endpoint refresh failed: %s", exc)
        await asyncio.sleep(_REFRESH_INTERVAL_S)


async def _start_account_refresher(
    app: web.Application,
) -> None:
    if _accounts.parse_spec(_config.ACCOUNT_CRED_PATHS):
        app["_account_refresher"] = asyncio.create_task(_account_refresh_loop())


async def _stop_account_refresher(app: web.Application) -> None:
    task = app.get("_account_refresher")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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
    app.on_startup.append(_start_account_refresher)
    app.on_cleanup.append(_stop_account_refresher)
