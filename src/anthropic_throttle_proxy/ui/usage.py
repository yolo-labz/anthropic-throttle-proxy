"""Usage panel (spec 380): a read-only view of a ``usage-summary.v1`` file.

Hidden unless ``THROTTLE_USAGE_SUMMARY_PATH`` is set. Failure-tolerant: an
unreadable or invalid file renders a one-line reason in the panel, never a
500, never the file's content. The file is read only by ``GET /ui/usage`` (in a
worker thread, cached on mtime+size) — ``/__throttle/health`` never touches it.

Every token class is its own column and its own bar segment; cache is never
folded into fresh input, and an unknown count renders as ``unknown``.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp_jinja2
from aiohttp import web

from .. import usage_summary as _us

ENV = "THROTTLE_USAGE_SUMMARY_PATH"
STALE_WARN_S = 3600
STALE_CRIT_S = 86400
SKEW_S = 60  # tolerated clock skew between producer, source and this host
_BRT = timezone(timedelta(hours=-3))  # Brazil has had no DST since 2019.
_TPL = "partials/usage.html"
COLUMNS = (
    ("requests", "requests"),
    ("fresh_input", "fresh input"),
    ("cache_write", "cache write"),
    ("cache_read", "cache read"),
    ("output", "output"),
    ("reasoning", "of which reasoning"),
)

# ponytail: one-entry cache keyed on (path, mtime_ns, size); enough for one file.
_cache: dict[str, Any] = {"key": None, "result": (None, None)}


def summary_path() -> Path | None:
    raw = os.environ.get(ENV, "").strip()
    return Path(raw) if raw else None


def enabled() -> bool:
    return summary_path() is not None


def _read(path: Path) -> tuple[_us.Summary | None, str | None]:
    try:
        st = path.stat()
    except OSError as exc:
        return None, f"summary file unreadable ({exc.__class__.__name__})"
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache["key"] != key:
        try:
            result: tuple[_us.Summary | None, str | None] = (_us.load(path), None)
        except _us.SummaryError as exc:
            result = (None, f"summary rejected: {exc}")
        _cache.update(key=key, result=result)
    return _cache["result"]


def _brt(dt: datetime) -> str:
    return dt.astimezone(_BRT).strftime("%d/%m/%Y %H:%M")


def _ago(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def compact(n: int | None) -> str:
    """``1.2M`` style; ``unknown`` for None (never ``0``)."""
    if n is None:
        return "unknown"
    for div, suffix in ((1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")):
        if n >= div:
            return f"{n / div:.1f}{suffix}"
    return str(n)


def bar(counts: dict[str, int | None]) -> dict[str, Any]:
    """Stacked-bar geometry (viewBox width 100) over the KNOWN additive classes.

    ``reasoning`` is a subset of ``output``, so it is never a segment — adding
    it would count those tokens twice.
    """
    known = {k: counts.get(k) for k in _us.ADDITIVE_CLASSES if counts.get(k) is not None}
    unknown = [k for k in _us.ADDITIVE_CLASSES if counts.get(k) is None]
    whole = sum(known.values())
    segments, x = [], 0.0
    for key in _us.ADDITIVE_CLASSES:
        v = known.get(key)
        if not v or not whole:
            continue
        w = 100.0 * v / whole
        segments.append({"key": key, "x": round(x, 3), "w": round(w, 3), "n": v})
        x += w
    return {"segments": segments, "unknown": unknown, "empty": not whole}


def _cells(get) -> list[dict[str, Any]]:
    out = []
    for key, _label in COLUMNS:
        value, unknown = get(key)
        out.append({"key": key, "value": value, "text": compact(value), "unknown": unknown})
    return out


def _section(rows: list[_us.Row]) -> dict[str, Any]:
    totals = _cells(lambda key: _us.total(rows, key))
    counts = {c["key"]: c["value"] for c in totals if c["key"] in _us.ADDITIVE_CLASSES}
    return {
        "rows": [
            {
                "provider": r.provider,
                "model": r.model,
                "pane": r.pane,
                "cells": _cells(lambda key, r=r: (r.value(key), 0)),
                "bar": bar(r.counts),
            }
            for r in sorted(rows, key=lambda r: (r.provider, r.model, r.pane or ""))
        ],
        "totals": totals,
        "bar": bar(counts),
    }


def freshness(summary: _us.Summary, now: float) -> tuple[str, float]:
    """``(level, age_s)`` of the SOURCE data, never of the file write.

    ``coverage.window_end`` is the source as-of time; ``generated_at`` is only
    when the producer wrote the file, so re-exporting a frozen receipt must not
    look fresh: age runs from ``min(generated_at, window_end)``. A source time
    after the write (or after now), beyond ``SKEW_S``, is impossible — that is
    ``invalid-source-time``, rendered as stale, never fresh.
    """
    end = summary.window_end.timestamp()
    written = summary.generated_at.timestamp()
    age = max(0.0, now - min(written, end))
    if end > written + SKEW_S or end > now + SKEW_S:
        return "invalid-source-time", age
    if age >= STALE_CRIT_S:
        return "crit", age
    return ("warn" if age >= STALE_WARN_S else "ok"), age


def view(now: float | None = None) -> dict[str, Any]:
    """Template context. ``state`` is ``hidden`` | ``error`` | ``ok``."""
    path = summary_path()
    if path is None:
        return {"state": "hidden"}
    summary, error = _read(path)
    if summary is None:
        return {"state": "error", "error": error}
    now = time.time() if now is None else now
    level, age = freshness(summary, now)
    return {
        "state": "ok",
        "as_of": _brt(summary.window_end),
        "as_of_iso": summary.window_end.astimezone(UTC).isoformat(),
        "generated_at": _brt(summary.generated_at),
        "age": _ago(age),
        "stale_level": level,
        "window_start": _brt(summary.window_start),
        "window_end": _brt(summary.window_end),
        "window_span": _ago((summary.window_end - summary.window_start).total_seconds()),
        "files_scanned": summary.files_scanned,
        "files_skipped": summary.files_skipped,
        "rejected_rows": summary.rejected_rows,
        "columns": COLUMNS,
        "turn": _section([r for r in summary.rows if r.role == "turn"]),
        "compaction": _section([r for r in summary.rows if r.role == "compaction"]),
    }


async def usage_partial(request: web.Request) -> web.Response:
    """GET /ui/usage — the polled panel body (empty when the env is unset)."""
    if not enabled():
        return web.Response(text="", content_type="text/html")
    ctx = await asyncio.to_thread(view)
    return aiohttp_jinja2.render_template(_TPL, request, {"usage": ctx})


def attach_usage(app: web.Application) -> None:
    """Mount the route; call after ``attach_ui`` (needs its jinja env)."""
    from .auth import guard  # the same opt-in remote gate as every other /ui route

    app.router.add_get("/ui/usage", guard(usage_partial))
    env = aiohttp_jinja2.get_env(app)
    env.globals["usage_panel_enabled"] = enabled
