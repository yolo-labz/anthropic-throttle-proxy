"""Spec 378: read-only consumer burn view. No write path.

``GET /__throttle/consumers`` (JSON) and ``GET /ui/consumers`` (an HTMX
fragment polled by the ``/ui`` dashboard) expose the in-memory project-share
ledger: per-project spend in the 5 h window and its entitlement
g_p = U·w_p/Σw_active (the allocator's existing allotment), plus a per-seat
OBSERVATION of who spent it, keyed by the sanitized ``x-anthropic-throttle-seat``
header (``project_share.seat_of``; absent → ``_direct``). The seat is never an
allocator key: nothing here feeds routing, limiter or pacing. Routes exist
and seats are recorded only when ``THROTTLE_CONSUMERS_VIEW=1``.
"""

from __future__ import annotations

import collections
import html
import os
import threading
import time
from typing import Any, Final

from aiohttp import web

from . import project_share as ps

ENV: Final[str] = "THROTTLE_CONSUMERS_VIEW"
# A client can mint a new seat id per request; distinct seats tracked per
# window are capped and the overflow lands in one bucket, so a spoofing client
# costs at most MAX_SEATS keys, never unbounded memory.
MAX_SEATS: Final[int] = 256
OTHER: Final[str] = "_other"
SEATS_PER_PROJECT: Final[int] = 3  # seats named in a project row; the rest is "+N"
UI_ROWS: Final[int] = 12


def enabled() -> bool:
    return os.environ.get(ENV, "0").strip() == "1"


def seat_key(sanitized: str) -> str:
    """``seat_of``'s ``-`` (absent or unusable) is the ``_direct`` seat.

    ``seat_of`` strips leading ``-._/``, so a client can produce neither ``-``
    nor ``_direct`` itself — both only come from a missing header.
    """
    return ps.DIRECT if sanitized == "-" else sanitized


class SeatLedger:
    """Process-local, bounded: per-seat list-$ and turns in the 5 h window."""

    def __init__(self, max_events: int = 50_000, max_seats: int = MAX_SEATS) -> None:
        self._lock = threading.Lock()
        self._events: collections.deque[tuple[float, str, str, float]] = collections.deque(
            maxlen=max_events
        )
        self._last: dict[str, float] = {}
        self._max_seats = max_seats

    def record(self, seat: str, project: str, usd: float, now: float | None = None) -> None:
        t = time.time() if now is None else now
        with self._lock:
            if seat not in self._last and len(self._last) >= self._max_seats:
                horizon = t - ps.WINDOW_S
                self._last = {s: v for s, v in self._last.items() if v >= horizon}
                if len(self._last) >= self._max_seats:
                    seat = OTHER
            self._last[seat] = t
            self._events.append((t, seat, project, max(usd, 0.0)))

    def rows(self, now: float | None = None) -> list[dict[str, Any]]:
        t = time.time() if now is None else now
        horizon = t - ps.WINDOW_S
        agg: dict[str, dict[str, Any]] = {}
        with self._lock:
            events = [e for e in self._events if e[0] >= horizon]
        for at, seat, project, usd in events:
            row = agg.setdefault(seat, {"usd": 0.0, "turns": 0, "last": at, "projects": {}})
            row["usd"] += usd
            row["turns"] += 1
            row["last"] = max(row["last"], at)
            row["projects"][project] = row["projects"].get(project, 0.0) + usd
        out = [
            {
                "seat": seat,
                "usd_5h": round(r["usd"], 4),
                "turns_5h": r["turns"],
                "last_seen_s": round(t - r["last"], 1),
                "project": max(r["projects"], key=r["projects"].__getitem__),
            }
            for seat, r in agg.items()
        ]
        out.sort(key=lambda r: (-r["usd_5h"], r["seat"]))
        return out


SEATS = SeatLedger()


def record_seat(headers: object, project: str | None, usd: float) -> None:
    """Hot-path hook; a no-op unless the view is enabled (default off)."""
    if enabled():
        SEATS.record(seat_key(ps.seat_of(headers)), project or ps.DIRECT, usd)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _seats_by_project(seats: list[dict[str, Any]]) -> dict[str, list[str]]:
    by: dict[str, list[str]] = {}
    for s in seats:  # already sorted by spend
        by.setdefault(s["project"], []).append(s["seat"])
    return by


def snapshot(
    share: ps.ProjectShare, now: float | None = None, seats: SeatLedger | None = None
) -> dict[str, Any]:
    """Read-only view of ``share``. Always contains a ``_direct`` row."""
    t = time.time() if now is None else now
    view = share.view(t)
    seat_rows = (SEATS if seats is None else seats).rows(t)
    by_project = _seats_by_project(seat_rows)
    rows: list[dict[str, Any]] = [
        {
            "project": r.project,
            "usd_5h": _r(r.usd),
            "share_5h": _r(r.used),
            "entitlement": _r(r.entitlement),
            "weight": r.weight,
            "over": r.over,
            "active": r.entitlement is not None,
            "seats": by_project.get(r.project, []),
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
                "seats": by_project.get(ps.DIRECT, []),
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
        # Observation only — never an allocator key (spec 378).
        "seats": seat_rows,
    }


async def consumers_json(_: web.Request) -> web.Response:
    return web.json_response(snapshot(ps.SHARE))


def _fmt(v: object) -> str:
    return "—" if v is None else html.escape(str(v))


def _usd(v: float | None) -> str:
    return "—" if v is None else f"${v:.2f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _seat_cell(names: list[str]) -> str:
    if not names:
        return '<span class="muted">—</span>'
    shown = " ".join(f"<code>{html.escape(n)}</code>" for n in names[:SEATS_PER_PROJECT])
    extra = len(names) - SEATS_PER_PROJECT
    return shown + (f' <span class="muted">+{extra}</span>' if extra > 0 else "")


def _table(head: tuple[str, ...], rows: list[str]) -> str:
    th = "".join(f'<th scope="col">{h}</th>' for h in head)
    return (
        f'<table class="projects-table"><thead><tr>{th}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _hidden(n: int, noun: str) -> str:
    return f'<p class="muted">+{n} smaller {noun} not shown</p>' if n > 0 else ""


def render_fragment(snap: dict[str, Any]) -> str:
    """Server-rendered fragment; styles come from the dashboard's tokens."""
    projects = snap["consumers"]
    seats = snap["seats"]
    project_rows = [
        ('<tr class="over">' if r["over"] else "<tr>")
        + f"<td><code>{_fmt(r['project'])}</code></td><td>{_usd(r['usd_5h'])}</td>"
        f"<td>{_pct(r['share_5h'])}</td><td>{_pct(r['entitlement'])}</td>"
        f"<td>{_fmt(r['weight'])}</td><td>{_seat_cell(r['seats'])}</td></tr>"
        for r in projects[:UI_ROWS]
    ]
    seat_rows = [
        f"<tr><td><code>{_fmt(s['seat'])}</code></td><td>{_usd(s['usd_5h'])}</td>"
        f"<td>{s['turns_5h']}</td><td><code>{_fmt(s['project'])}</code></td>"
        f"<td>{int(s['last_seen_s'])} s ago</td></tr>"
        for s in seats[:UI_ROWS]
    ]
    w5, w7 = snap["window_5h"], snap["window_7d"]
    seat_block = (
        _table(("seat", "list $ this window", "turns", "top project", "last seen"), seat_rows)
        + _hidden(len(seats) - UI_ROWS, "seats")
        if seats
        else '<p class="muted">no seat header observed this window — '
        "every turn is <code>_direct</code></p>"
    )
    return (
        '<div class="projects-card">'
        '<div class="projects-head"><span><b>Consumers</b> · read-only</span>'
        f"<span>5 h util <b>{_pct(w5['util'])}</b></span>"
        f"<span>7 d util <b>{_pct(w7['util'])}</b></span>"
        f"<span>list $ this window <b>{_usd(w5['total_usd'])}</b></span></div>"
        + _table(
            ("project", "list $ this window", "used", "fair share g", "weight", "seats seen"),
            project_rows,
        )
        + _hidden(len(projects) - UI_ROWS, "projects")
        + '<p class="muted">seats: observed from <code>x-anthropic-throttle-seat</code> — '
        "observation only, never an allocator key; absent header = <code>_direct</code></p>"
        + seat_block
        + "</div>"
    )


async def consumers_html(_: web.Request) -> web.Response:
    return web.Response(text=render_fragment(snapshot(ps.SHARE)), content_type="text/html")


def attach_consumers(app: web.Application) -> None:
    """Register the read-only routes iff the env flag is on (default off)."""
    if not enabled():
        return
    app.router.add_get("/__throttle/consumers", consumers_json)
    app.router.add_get("/ui/consumers", consumers_html)
