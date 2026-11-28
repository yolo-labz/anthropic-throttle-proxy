"""Read-only provider/model attribution over the existing usage-summary.v1 loader.

This is a retained aggregate, not an event stream or an account/quota join.
The geometry and the exact table share one filtered row set. Every lane uses
the same absolute scale, derived from the WHOLE source, including when filters
change. No collectors, network calls, capture, or runtime mutations live here.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import aiohttp_jinja2
from aiohttp import web

from .. import usage_summary as us
from . import usage

MEASURES = (
    ("tokens", "Accounted tokens"),
    ("requests", "Recorded calls"),
    ("fresh_input", "Fresh input tokens"),
    ("cache_write", "Cache write tokens"),
    ("cache_read", "Cache read tokens"),
    ("output", "Output tokens"),
    ("reasoning", "Reasoning tokens · within output"),
)
ROLES = (("", "All recorded calls"), ("turn", "Model turns"), ("compaction", "Compaction"))
COLUMNS = (
    ("requests", "Recorded calls"),
    ("fresh_input", "Fresh input"),
    ("cache_write", "Cache write"),
    ("cache_read", "Cache read"),
    ("output", "Output"),
    ("reasoning", "Of which reasoning"),
)
_FILTER_DEFAULTS = {"provider": "", "model": "", "role": "", "measure": "tokens"}
_UNSUPPORTED_SELECTIONS = ("account", "from", "to", "pane", "resolution")
_LOADED_SHA256 = re.compile(r"[0-9a-f]{64}")
CLASS_LABELS = dict(usage.COLUMNS)
FRESHNESS_LABELS = {
    "ok": "Source within one hour",
    "warn": "Source older than one hour",
    "crit": "Source older than one day",
    "invalid-source-time": "Source clock inconsistent",
}


def _stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%d %b %Y, %H:%M:%S UTC")


def _cell(values: list[int | None]) -> dict[str, Any]:
    known = [n for n in values if n is not None]
    value = sum(known) if known else None
    unknown = len(values) - len(known)
    prefix = "≥ " if value is not None and unknown else ""
    return {
        "value": value,
        "unknown": unknown,
        "text": prefix + (f"{value:,}" if value is not None else "unknown"),
        "compact": prefix + usage.compact(value),
    }


def _metric(rows: list[us.Row], measure: str) -> dict[str, Any]:
    keys = us.ADDITIVE_CLASSES if measure == "tokens" else (measure,)
    return _cell([r.value(key) for r in rows for key in keys])


def _cells(rows: list[us.Row]) -> list[dict[str, Any]]:
    return [{"key": key, **_metric(rows, key)} for key, _ in COLUMNS]


def _group(rows: list[us.Row]) -> dict[tuple[str, str], list[us.Row]]:
    groups: dict[tuple[str, str], list[us.Row]] = defaultdict(list)
    for row in rows:
        groups[row.provider, row.model].append(row)
    return groups


def _scale(rows: list[us.Row], measure: str) -> int:
    """A fixed, linear full-source scale; never normalize individual lanes."""
    maximum = max((_metric(rs, measure)["value"] or 0 for rs in _group(rows).values()), default=0)
    if maximum <= 4:
        return 4
    magnitude = 10 ** (len(str(maximum)) - 1)
    return next(n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= maximum)


def _tick_label(maximum: int, quarter: int) -> str:
    """Exact quarter labels, including 2.5 on a ten-call scale."""
    whole, remainder = divmod(maximum * quarter, 4)
    return f"{whole}.{remainder * 25:02d}".rstrip("0") if remainder else usage.compact(whole)


def _geometry(rows: list[us.Row], measure: str, maximum: int) -> list[dict[str, Any]]:
    """Known volume only. No minimum-width marks that invent tiny volume."""
    keys = us.ADDITIVE_CLASSES if measure == "tokens" else (measure,)
    parts, offset = [], 0
    for key in keys:
        cell = _metric(rows, key)
        n = cell["value"]
        if n is not None and n > 0:
            parts.append(
                {
                    "key": key,
                    "label": CLASS_LABELS.get(key, "recorded calls"),
                    "x": 1000 * offset / maximum,
                    "width": 1000 * n / maximum,
                    "text": cell["text"],
                }
            )
            offset += n
    return parts


def _options(query: Mapping[str, str], summary: us.Summary) -> dict[str, str]:
    selected = {key: query.get(key, default) for key, default in _FILTER_DEFAULTS.items()}
    allowed = {
        "provider": {"", *(r.provider for r in summary.rows)},
        "model": {"", *(r.model for r in summary.rows)},
        "role": {key for key, _ in ROLES},
        "measure": {key for key, _ in MEASURES},
    }
    for key, value in selected.items():
        if value not in allowed[key]:
            raise ValueError(f"Unknown {key} filter; return to the unfiltered Observatory.")
    return selected


def build_view(summary: us.Summary, query: Mapping[str, str], *, now: float) -> dict[str, Any]:
    """Pure, deterministic projection shared by the live route and fixtures."""
    selected = _options(query, summary)
    measure = selected["measure"]
    rows = [
        row
        for row in summary.rows
        if all(
            not selected[key] or getattr(row, key) == selected[key]
            for key in ("provider", "model", "role")
        )
    ]
    rows.sort(key=lambda r: (r.provider, r.model, r.role, r.pane or ""))
    maximum = _scale(summary.rows, measure)
    groups = _group(rows)
    providers = []
    for provider in sorted({r.provider for r in rows}):
        provider_rows = [r for r in rows if r.provider == provider]
        models = []
        for (name, model), model_rows in sorted(groups.items()):
            if name != provider:
                continue
            metric = _metric(model_rows, measure)
            models.append(
                {
                    "model": model,
                    "metric": metric,
                    "segments": _geometry(model_rows, measure, maximum),
                    "source_rows": len(model_rows),
                }
            )
        providers.append(
            {"provider": provider, "metric": _metric(provider_rows, measure), "models": models}
        )
    level, age = usage.freshness(summary, now)
    # The existing loader validates coverage skew. A future publication time
    # must also be explicit here, even when coverage itself is in the past.
    if summary.generated_at.timestamp() > now + usage.SKEW_S:
        level = "invalid-source-time"
    observed = min(summary.generated_at, summary.window_end)
    measure_label = dict(MEASURES)[measure]
    unit = "recorded calls" if measure == "requests" else "tokens"
    source_omissions = bool(summary.files_skipped or summary.rejected_rows)
    total = _metric(rows, measure)
    headline = total["compact"]
    if total["value"] is not None and (source_omissions or total["unknown"]):
        # Do not round a lower bound up or attach source omissions to individual groups.
        headline = f"≥ {total['value']:,}"
    return {
        "state": "ok",
        "selected": selected,
        "provider_options": sorted({r.provider for r in summary.rows}),
        "model_options": sorted({r.model for r in summary.rows}),
        "measure_label": measure_label,
        "unit": unit,
        "total": total,
        "headline": headline,
        "source_omissions": source_omissions,
        "totals": _cells(rows),
        "providers": providers,
        "model_count": len(groups),
        "provider_count": len(providers),
        "matched_rows": len(rows),
        "source_rows": len(summary.rows),
        "scale": maximum,
        "ticks": [{"position": i * 25, "text": _tick_label(maximum, i)} for i in range(5)],
        "legend": [(key, label) for key, label in COLUMNS if key in us.ADDITIVE_CLASSES]
        if measure == "tokens"
        else [(measure, measure_label)],
        "table_rows": [
            {
                "provider": row.provider,
                "model": row.model,
                "role": dict(ROLES)[row.role],
                "pane": row.pane or "unattributed",
                "cells": _cells([row]),
            }
            for row in rows
        ],
        "freshness": level,
        "freshness_label": FRESHNESS_LABELS[level],
        "source_age": usage._ago(age),
        "observed_at": _stamp(observed),
        "observed_iso": observed.astimezone(UTC).isoformat(),
        "generated_at": _stamp(summary.generated_at),
        "generated_iso": summary.generated_at.astimezone(UTC).isoformat(),
        "loaded_sha256": summary.loaded_sha256,
        "window_start": _stamp(summary.window_start),
        "window_end": _stamp(summary.window_end),
        "window_span": usage._ago((summary.window_end - summary.window_start).total_seconds()),
        "files_scanned": summary.files_scanned,
        "files_skipped": summary.files_skipped,
        "rejected_rows": summary.rejected_rows,
        "captured_at": _stamp(datetime.fromtimestamp(now, UTC)),
    }


class _PinnedSummaryUnavailable(Exception):
    """The existing loader cannot resolve the requested immutable generation."""


def view(
    query: Mapping[str, str], now: float | None = None, *, frozen_sha256: str | None = None
) -> dict[str, Any]:
    """Reuse the bounded, stat-keyed loader; called in a worker thread."""
    path = usage.summary_path()
    if path is None:
        if frozen_sha256 is not None:
            raise _PinnedSummaryUnavailable
        return {
            "state": "missing",
            "error": "No published usage summary is configured for this process.",
        }
    summary, error = usage._read(path)
    if frozen_sha256 is not None:
        # ponytail: compare only the loaded digest in the existing read. No archive or fallback.
        if summary is None or summary.loaded_sha256 != frozen_sha256:
            raise _PinnedSummaryUnavailable
        pinned = build_view(summary, query, now=summary.generated_at.timestamp())
        pin = {"mode": "frozen", "as_of": frozen_sha256}
        pinned.update(
            frozen=True,
            captured_at=None,
            source_age=None,
            clear_url="/ui/observatory?" + urlencode(pin),
            current_url="/ui/observatory?" + urlencode(pinned["selected"]),
        )
        return pinned
    if summary is None:
        return {"state": "error", "error": error}
    current = build_view(summary, query, now=time.time() if now is None else now)
    if summary.loaded_sha256:
        selection = current["selected"] | {"mode": "frozen", "as_of": summary.loaded_sha256}
        current["freeze_url"] = "/ui/observatory?" + urlencode(selection)
    return current


def _query_selections(request: web.Request) -> tuple[dict[str, str], str | None]:
    query = request.query
    for key in _UNSUPPORTED_SELECTIONS:
        if key in query:
            raise ValueError(
                f"Unsupported Observatory selection: {key}. Use provider, model, role, or measure."
            )

    modes = query.getall("mode", [])
    digests = query.getall("as_of", [])
    frozen_sha256 = None
    if modes:
        if modes != ["frozen"] or not digests:
            raise ValueError(
                "Unsupported Observatory selection: mode. Frozen mode requires one as_of SHA-256."
            )
        if len(digests) != 1 or not _LOADED_SHA256.fullmatch(digests[0]):
            raise ValueError("Invalid Observatory as_of: expected one lowercase SHA-256.")
        frozen_sha256 = digests[0]
    elif digests:
        raise ValueError("Unsupported Observatory selection: as_of. Use mode=frozen.")

    # Other parameters are not selection controls (for example, cache busters).
    selected = {}
    for key in _FILTER_DEFAULTS:
        values = query.getall(key, [])
        if len(set(values)) > 1:
            raise ValueError(f"Conflicting {key} filter values. Send one value per filter.")
        if values:
            selected[key] = values[0]
    return selected, frozen_sha256


async def index(request: web.Request) -> web.Response:
    """GET-only snapshot. Inspection never starts background refresh or traffic."""
    try:
        selected, frozen_sha256 = _query_selections(request)
        if frozen_sha256 is None:
            context = await asyncio.to_thread(view, selected)
        else:
            context = await asyncio.to_thread(view, selected, frozen_sha256=frozen_sha256)
    except _PinnedSummaryUnavailable:
        raise web.HTTPNotFound(
            text="Pinned Observatory summary unavailable or expired.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc), headers={"Cache-Control": "no-store"}) from None
    from .routes import _ASSET_V

    response = aiohttp_jinja2.render_template(
        "observatory.html",
        request,
        {
            "ob": context,
            "measures": MEASURES,
            "roles": ROLES,
            "columns": COLUMNS,
            "asset_v": _ASSET_V,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def attach_observatory(app: web.Application) -> None:
    from .auth import guard

    app.router.add_get("/ui/observatory", guard(index))
