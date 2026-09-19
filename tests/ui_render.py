"""Render ``partials/stats.html`` from a test, without a server.

Two test modules need the same thing — a real Jinja render of the live panel
with a minimal context — and the code-slop gate is right to object when they
each keep their own copy of it: a template context that lives in two places
drifts, and then one suite silently stops exercising the panel the other one
does.

**Strict by default.** The environment uses ``jinja2.StrictUndefined``, so a
page-level key the template reads and the fixture does not supply raises
instead of rendering an empty string. That is how a template context rots: the
panel grows a key, one test's fixture grows it, another's does not, and the
second suite spends a year asserting against an empty cell.

**Explicit about optionality.** The per-row keys that legitimately vary — the
ones a builder sets only when it has something to say — are listed once, below,
and merged under the row the caller passes. Everything else on a row is
required and must be named by the fixture.

Not named `test_*`, so pytest does not collect it.
"""

from __future__ import annotations

import jinja2

from anthropic_throttle_proxy.ui import routes

# Page-level keys the panel reads. `identity` is grouped here rather than
# defaulted in the template because `_collect_view` always supplies it and the
# banner it gates is a real state, not a missing one.
_EMPTY_CONTEXT: dict[str, object] = {
    "subscriptions": [],
    "bearers": [],
    "providers": [],
    "signals": [],
    "status": None,
    "lanes": None,
    "identity": None,
    "last_advisor": None,
    "fleet_ui_config_error": None,
    "copilot": [],
    "served": 0,
    "inflight": 0,
    "queued": 0,
    "holds": 0,
    "retries": 0,
    "disconnects": 0,
}

# Per-row keys a builder emits only when it has something to say. Required on
# every row: `id`, `identity`, `family`, `status`, `meters`, `pace`, `eta`,
# `plan`, `src`, `sub` — a fixture that leaves one of those out renders
# nothing for it, which is the failure this file exists to prevent.
_OPTIONAL_ROW: dict[str, object] = {
    "icon": "",
    "label": "",
    "sub": "",
    "status_icon": "",
    "detail": "",
    "pace": None,
    "pace_warn": False,
    "eta": "",
    "src": "",
    "billing": None,
    "account_badge": "",
    "plan_caption": "",
    "plan_conflict": False,
    "is_binding": False,
    "routing_eligible": False,
}

# Same idea for the other three row shapes the panel renders.
_OPTIONAL_METER: dict[str, object] = {
    "icon": "",
    "label": "?",
    "pct": None,
    "reset_in": "",
    "reset_at": "",
    "resets_at": None,
    "note": "",
    "rejected": False,
    "exhausted_ok": False,
}

_OPTIONAL_PROVIDER: dict[str, object] = {
    "icon": "",
    "kind": "primary",
    "ok": True,
    "level": "idle",
    "upstream": "",
    "served": 0,
    "inflight": 0,
    "queued": 0,
    "max_concurrent": 0,
    "egress_ok": None,
    "dns_ok": None,
    "auth_dead": False,
    "err": "",
}

_OPTIONAL_BEARER: dict[str, object] = {
    "identity": "",
    "account": "",
    "inflight": 0,
    "queued": 0,
    "served": 0,
    "unified": None,
    "unified_5h_stale": False,
    "last_ratelimit": None,
    "retry_after_in": "",
    "limiter": None,
}

_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(routes._TEMPLATES)),
    autoescape=True,
    # See the module docstring: the first version of this helper used the
    # default `Undefined` while its docstring promised otherwise, so a missing
    # simple value rendered empty and a missing conditional evaluated false
    # (cross-family review, 18/09/2026).
    undefined=jinja2.StrictUndefined,
)


def render_stats(**over: object) -> str:
    """Render the live panel with the empty context plus ``over``.

    Row lists passed here are merged under the per-shape optional keys, so a
    caller only names the fields its assertion is about.
    """
    context: dict[str, object] = {**_EMPTY_CONTEXT, **over}
    shapes = (
        ("subscriptions", _OPTIONAL_ROW, "meters"),
        ("providers", _OPTIONAL_PROVIDER, ""),
        ("bearers", _OPTIONAL_BEARER, ""),
    )
    for key, defaults, nested in shapes:
        given = context.get(key)
        if not isinstance(given, list):
            continue
        merged: list[object] = []
        for row in given:
            if not isinstance(row, dict):
                merged.append(row)
                continue
            filled = {**defaults, **row}
            meters = filled.get(nested)
            if nested and isinstance(meters, list):
                filled[nested] = [{**_OPTIONAL_METER, **meter} for meter in meters]
            merged.append(filled)
        context[key] = merged
    return _ENV.get_template("partials/stats.html").render(**context)


def render_subscription(row: dict) -> str:
    """Render one subscription row through the real template.

    A row must name its `id`, `identity`, `family`, `status` and `plan`; the
    rest are declared optional in `_OPTIONAL_ROW`, because the builder sets them
    only when it has something to say.
    """
    return render_stats(subscriptions=[row])


def render_meter(meter: dict) -> str:
    """Render one subscription row's meters cell through the real template."""
    return render_subscription(
        {
            "id": "x",
            "identity": "x",
            "family": "anthropic",
            "plan": "",
            "src": "proxy",
            "meters": [meter],
            "status": "rejected" if meter.get("rejected") else "ok",
        }
    )
