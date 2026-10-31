"""Spec 378 phase 1: read-only consumer burn view. No write path.

``GET /__throttle/consumers`` (JSON) and ``GET /ui/consumers`` (HTML table)
expose the in-memory project-share ledger: per-project spend in the 5 h
window and its entitlement g_p = U·w_p/Σw_active (the allocator's existing
allotment). Routes exist only when ``THROTTLE_CONSUMERS_VIEW=1``.
"""

from __future__ import annotations

import html
import os
import time
from typing import Any, Final

from aiohttp import web

from . import project_share as ps

ENV: Final[str] = "THROTTLE_CONSUMERS_VIEW"


def enabled() -> bool:
    return os.environ.get(ENV, "0").strip() == "1"


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def snapshot(share: ps.ProjectShare, now: float | None = None) -> dict[str, Any]:
    """Read-only view of ``share``. Always contains a ``_direct`` row."""
    t = time.time() if now is None else now
    view = share.view(t)
    rows: list[dict[str, Any]] = [
        {
            "project": r.project,
            "usd_5h": _r(r.usd),
            "share_5h": _r(r.used),
            "entitlement": _r(r.entitlement),
            "weight": r.weight,
            "over": r.over,
            "active": r.entitlement is not None,
        }
        for r in view.rows
    ]
    if not any(r["project"] == ps.DIRECT for r in rows):
        # Absent / unusable header lands here: the unattributed bucket is always shown.
        rows.append(
            {
                "project": ps.DIRECT,
                "usd_5h": 0.0,
                "share_5h": None,
                "entitlement": None,
                "weight": share.weight(ps.DIRECT),
                "over": False,
                "active": False,
            }
        )
    return {
        "generated_at": t,
        "usable": ps.USABLE,
        "window_5h": {
            "util": _r(view.util_5h),
            "reset": view.reset_5h,
            "state": view.state,
            "total_usd": _r(view.total_usd),
            "capacity_usd": _r(view.capacity_usd),
        },
        "window_7d": {
            "util": _r(view.util_7d),
            "reset": view.reset_7d,
            "usd": _r(view.week_usd),
            "capacity_usd": _r(view.week_capacity_usd),
        },
        "consumers": rows,
    }


async def consumers_json(_: web.Request) -> web.Response:
    return web.json_response(snapshot(ps.SHARE))


_CELL = "<td>{}</td>"


def _fmt(v: object) -> str:
    return "—" if v is None else html.escape(str(v))


async def consumers_html(_: web.Request) -> web.Response:
    snap = snapshot(ps.SHARE)
    head = "".join(
        f"<th>{h}</th>"
        for h in ("project", "usd 5h", "share 5h", "entitlement", "weight", "over", "active")
    )
    body = "".join(
        "<tr>"
        + "".join(
            _CELL.format(_fmt(r[k]))
            for k in ("project", "usd_5h", "share_5h", "entitlement", "weight", "over", "active")
        )
        + "</tr>"
        for r in snap["consumers"]
    )
    page = (
        "<!doctype html><meta charset=utf-8><title>consumers</title>"
        "<h1>Consumers (read-only)</h1>"
        f"<p>5h util {_fmt(snap['window_5h']['util'])} "
        f"· 7d util {_fmt(snap['window_7d']['util'])}</p>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )
    return web.Response(text=page, content_type="text/html")


def attach_consumers(app: web.Application) -> None:
    """Register the read-only routes iff the env flag is on (default off)."""
    if not enabled():
        return
    app.router.add_get("/__throttle/consumers", consumers_json)
    app.router.add_get("/ui/consumers", consumers_html)
