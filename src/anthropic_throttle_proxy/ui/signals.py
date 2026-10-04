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

__all__ = ["Signal", "Spark", "TpsGauge", "collect", "sparkline", "tps_gauge"]

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


class TpsGauge(NamedTuple):
    """Geometry + numbers for the tokens/s arc gauge.

    Server-side math only; the template places values. The arc is a 180°
    speedometer of radius 90 whose sweep length is π·r, so the value arc is a
    `stroke-dasharray` fraction of that and the peak marker/ticks are plain
    coordinate pairs — no charting library, per the dashboard invariant.
    """

    value: float  # current tokens/s over the trailing window
    peak: float  # highest windowed tokens/s in the ring
    scale: float  # arc full-scale (nice 1/2/5·10ⁿ ≥ peak floor)
    frac: float  # value/scale, clamped to [0, 1]
    peak_frac: float  # peak/scale, clamped to [0, 1]
    arc_len: float  # full sweep length in SVG units
    window_s: float
    ticks: list[tuple[float, float, float, float]]  # (x1, y1, x2, y2) at 0/25/50/75/100%
    peak_marker: tuple[float, float, float, float]  # small radial line at peak
    spark: Spark  # tokens/s trace, same treatment as the four signals
    tok_in_now: float  # trailing-window input tokens/s (input + cache reads)
    tok_in_fresh: float | None  # trailing-window FRESH input tokens/s (cache
    # excluded). None = not measured (e.g. 2-field sibling snapshots) — never
    # rendered as a measured zero.
    seen: bool  # False until the ring holds a bucket with any token traffic


# Arc geometry — one place, so the template never recomputes it.
_ARC_CX, _ARC_CY, _ARC_R = 110.0, 108.0, 90.0
_ARC_LEN = math.pi * _ARC_R
_TPS_WINDOW_BUCKETS = 6  # 6 × 10 s = the 60 s the headline number averages
_TPS_FLOOR = 100.0  # arcs need a full-scale even when nothing has flowed yet


def _nice_scale(v: float, floor: float) -> float:
    """Smallest 1/2/5·10ⁿ ≥ max(v, floor) — arc labels stay round."""
    target = max(v, floor)
    if target <= 0:
        return floor
    exp = math.floor(math.log10(target))
    for mult in (1.0, 2.0, 5.0):
        cand = mult * (10.0**exp)
        if cand >= target:
            return cand
    return 10.0 ** (exp + 1)


def _arc_point(frac: float, r_out: float) -> tuple[float, float]:
    """Point at `frac` of the sweep, measured from the left end (180° → 0°)."""
    f = min(1.0, max(0.0, frac))
    angle = math.pi * (1.0 - f)
    return (_ARC_CX + r_out * math.cos(angle), _ARC_CY - r_out * math.sin(angle))


def remote_tps(snapshot: object) -> TpsGauge | None:
    """Accept bounded, measured token buckets; absent/old/bad telemetry is unknown."""
    if not isinstance(snapshot, dict) or snapshot.get("bucket_seconds") != _history.RESOLUTION_S:
        return None
    buckets = snapshot.get("tokens")
    if not isinstance(buckets, list) or len(buckets) > 360:
        return None
    for pair in buckets:
        if not isinstance(pair, list) or len(pair) not in (2, 3):
            return None
        if any(type(v) is not int or not 0 <= v <= 2**53 for v in pair):
            return None
    return tps_gauge(buckets)


def tps_gauge(token_buckets: list | None = None) -> TpsGauge:
    """Build the gauge from local history or validated sibling token buckets."""
    buckets = token_buckets
    if buckets is None:
        buckets = [(p.tok_out, p.tok_in, p.tok_in_fresh) for p in _history.series()]
    rates = [p[0] / _history.RESOLUTION_S for p in buckets]
    in_rates = [p[1] / _history.RESOLUTION_S for p in buckets]
    fresh_rates = [p[2] / _history.RESOLUTION_S for p in buckets if len(p) > 2]
    # Windowed mean over the last N closed buckets: token usage lands at
    # completion, so a single 10 s bucket is a lumpy estimator. Mean of the
    # trailing 60 s is what the number claims to be (“current” = last minute).
    n = min(_TPS_WINDOW_BUCKETS, len(rates))
    value = (sum(rates[-n:]) / n) if n else 0.0
    tok_in_now = (sum(in_rates[-n:]) / n) if n else 0.0
    # Fresh is a SEPARATE figure beside the total, never a replacement for it:
    # measured only when every bucket carried it (304 follow-up).
    tok_in_fresh = (sum(fresh_rates[-n:]) / n) if n and len(fresh_rates) == len(buckets) else None
    peak = max(rates) if rates else 0.0
    scale = _nice_scale(peak, _TPS_FLOOR)
    frac = min(1.0, value / scale) if scale else 0.0
    peak_frac = min(1.0, peak / scale) if scale else 0.0

    ticks = []
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        x1, y1 = _arc_point(t, _ARC_R - 8.0)
        x2, y2 = _arc_point(t, _ARC_R - 2.0)
        ticks.append((round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)))
    mx1, my1 = _arc_point(peak_frac, _ARC_R - 12.0)
    mx2, my2 = _arc_point(peak_frac, _ARC_R + 6.0)

    return TpsGauge(
        value=value,
        peak=peak,
        scale=scale,
        frac=frac,
        peak_frac=peak_frac,
        arc_len=_ARC_LEN,
        window_s=_TPS_WINDOW_BUCKETS * _history.RESOLUTION_S,
        ticks=ticks,
        peak_marker=(round(mx1, 1), round(my1, 1), round(mx2, 1), round(my2, 1)),
        spark=sparkline(_fold(rates, peaks=True)),
        tok_in_now=tok_in_now,
        tok_in_fresh=tok_in_fresh,
        seen=any(p[0] > 0 for p in buckets),
    )


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
