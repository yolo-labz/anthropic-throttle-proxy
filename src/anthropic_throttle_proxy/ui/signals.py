"""Turn the history ring into the header's four signal traces.

RED / four-golden-signals says the honest header for a service is *rate,
errors, duration, saturation* — as series, not scalars (Grafana's own
dashboard guidance; `docs/DASHBOARD-DESIGN.md`). This module does the
arithmetic and the sparkline geometry; the template only places them.

Sparklines are server-rendered `<svg><polyline>` — no charting library, no
JavaScript module, per the dashboard's standing invariant.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from .. import history as _history

__all__ = ["Signal", "Spark", "collect", "sparkline"]

SPARK_W = 96.0
SPARK_H = 20.0
# The ring holds 360 buckets; a ~96 px trace cannot resolve them, and drawing
# all of them turns a sporadic-pushback series into an unreadable barcode.
# Fold to one point per minute before plotting.
_TRACE_POINTS = 60
# Trailing average window for the headline number: 6 × 10 s buckets = 1 min,
# so "requests/min" is literally the last minute, not an extrapolated tick.
_TRAILING = 6


class Spark(NamedTuple):
    """Geometry for one inline trace."""

    points: str  # SVG polyline `points` attribute
    peak: float  # y-scale maximum, so the trace can be read against something
    width: float = SPARK_W
    height: float = SPARK_H


class Signal(NamedTuple):
    """One header cell: a label, a trace, a current value, and its scale."""

    key: str
    label: str
    value: str
    detail: str
    level: str  # "" | "warn" | "crit" — drives the accent, never the only cue
    spark: Spark


def sparkline(values: list[float], width: float = SPARK_W, height: float = SPARK_H) -> Spark:
    """Normalise ``values`` into polyline geometry with a zero baseline.

    The y-scale starts at 0 (not at min) so a flat-but-high trace reads as
    high rather than as noise, and the peak is returned for the caller to
    label — a sparkline whose scale is invisible is decoration.
    """
    if not values:
        return Spark(points="", peak=0.0, width=width, height=height)
    peak = max(values)
    if peak <= 0:
        peak = 1.0
    if len(values) == 1:
        values = [values[0], values[0]]
    step = width / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        x = i * step
        y = height - (max(0.0, v) / peak) * height
        pts.append(f"{x:.1f},{y:.1f}")
    return Spark(points=" ".join(pts), peak=peak, width=width, height=height)


def _trailing(values: list[float], n: int = _TRAILING) -> float:
    tail = values[-n:]
    return sum(tail) / len(tail) if tail else 0.0


def _fold(values: list[float], peaks: bool = False, points: int = _TRACE_POINTS) -> list[float]:
    """Fold the raw buckets into ``points`` plot points, newest-aligned.

    ``peaks=True`` keeps the maximum of each group instead of its mean — for
    errors and latency, where a spike swallowed by an average is the whole
    signal.
    """
    if len(values) <= points:
        return values
    size = math.ceil(len(values) / points)
    groups = [values[i : i + size] for i in range(0, len(values), size)]
    return [max(g) if peaks else sum(g) / len(g) for g in groups]


def _rate_series(points: list[_history.Point]) -> tuple[list[float], list[float]]:
    """Per-minute request and pushback rates, one entry per closed bucket."""
    per_min = 60.0 / _history.RESOLUTION_S
    return (
        [p.served * per_min for p in points],
        [p.errors * per_min for p in points],
    )


def _saturation_series(points: list[_history.Point]) -> list[float]:
    """Demand (in-flight + queued) as a percentage of the live AIMD cap.

    Above 100% means the queue is holding work the cap cannot dispatch — the
    saturation signal, and the one that precedes every queue-timeout 503.
    """
    out = []
    for p in points:
        demand = p.inflight + p.queued
        if p.cap > 0:
            out.append(demand / p.cap * 100.0)
        else:
            # No cap AND work waiting is total saturation, not 0% — reading it
            # as healthy would hide the exact state this signal exists for
            # (every bearer paused on a Retry-After, nothing dispatchable).
            out.append(100.0 if demand else 0.0)
    return out


def _fmt_rate(v: float) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


def _fmt_ms(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.1f}s"


def _level(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return "crit"
    if value >= warn:
        return "warn"
    return ""


def collect() -> list[Signal]:
    """Build the four header signals from the current history ring."""
    points = _history.series()
    served, errors = _rate_series(points)
    latency = [p.p95 or 0.0 for p in points]
    saturation = _saturation_series(points)

    rate_now = _trailing(served)
    err_now = _trailing(errors)
    sat_now = _trailing(saturation)
    p95_seen = [p.p95 for p in points[-_TRAILING:] if p.p95 is not None]
    p95_now = max(p95_seen) if p95_seen else None
    span = f"{len(points) * _history.RESOLUTION_S / 60:.0f}m" if points else "no history yet"

    return [
        Signal(
            key="rate",
            label="requests / min",
            value=_fmt_rate(rate_now),
            detail=f"peak {_fmt_rate(max(served) if served else 0)} · {span}",
            level="",
            spark=sparkline(_fold(served)),
        ),
        Signal(
            key="errors",
            label="pushback / min",
            value=_fmt_rate(err_now),
            detail=f"peak {_fmt_rate(max(errors) if errors else 0)} · 429/503/529",
            level=_level(err_now, 1.0, 6.0),
            spark=sparkline(_fold(errors, peaks=True)),
        ),
        Signal(
            key="latency",
            label="upstream p95",
            value=_fmt_ms(p95_now),
            detail=f"p50 {_fmt_ms(next((p.p50 for p in reversed(points) if p.p50), None))}",
            level="",
            spark=sparkline(_fold(latency, peaks=True)),
        ),
        Signal(
            key="saturation",
            label="saturation",
            value=f"{sat_now:.0f}%",
            detail="(in-flight + queued) ÷ live cap",
            level=_level(sat_now, 80.0, 100.0),
            spark=sparkline(_fold(saturation)),
        ),
    ]
