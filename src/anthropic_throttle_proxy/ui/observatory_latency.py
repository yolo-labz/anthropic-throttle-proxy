"""Project admitted per-window classic latency histograms without loading data.

The caller owns admission: exact opening/closing scrapes, the coverage cutoff,
and continuity within one emitter/replica/reset segment. The projector checks
compatibility with a supplied track and never subtracts across time windows.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite
from typing import Literal

from .observatory_evidence import (
    Evidence,
    Generation,
    Scope,
    SelectionRelation,
    Window,
    selection_relation,
)

type BucketBound = float | Literal["+Inf"]
type Status = Literal["observed", "missing", "unknown", "rejected"]

COUNT_MEASURE = "latency_observations"
COUNT_UNIT = "count"
BOUNDS_SECONDS: tuple[BucketBound, ...] = (
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    25,
    60,
    120,
    300,
    600,
    "+Inf",
)


@dataclass(frozen=True)
class Bucket:
    upper_bound_seconds: BucketBound
    cumulative_count: int


@dataclass(frozen=True)
class SeriesGeneration:
    collector_generation: str
    scrape_configuration: str
    histogram_schema_revision: str
    canonical_emitter: str
    canonical_replica: str
    exporter_reset_segment: str


@dataclass(frozen=True)
class HistogramWindow:
    source_target: str
    series_generation: SeriesGeneration
    window: Window
    buckets: tuple[Bucket, ...]
    count: int
    sum_seconds: float


@dataclass(frozen=True)
class Reason:
    code: str
    message: str


@dataclass(frozen=True)
class HistogramResult:
    status: Status
    value: HistogramWindow | None
    evidence: Evidence
    reason: Reason | None = None


@dataclass(frozen=True)
class HistogramTrack:
    """Explicit selection binding, not inferred from the first value supplied."""

    scope: Scope
    source_target: str
    series_generation: SeriesGeneration
    publication_generation: Generation


@dataclass(frozen=True)
class LatencyBin:
    # Lower exclusive (or none for the first bound); upper inclusive (or open tail).
    lower_seconds: float | None
    upper_seconds: float | None


@dataclass(frozen=True)
class HeatmapRow:
    window: Window
    status: Status
    evidence: Evidence | None = None
    counts: tuple[int, ...] | None = None
    count: int | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class LatencyHeatmap:
    bins: tuple[LatencyBin, ...]
    rows: tuple[HeatmapRow, ...]
    peak_count: int


_EDGES = (None, *(bound for bound in BOUNDS_SECONDS if bound != "+Inf"), None)
_BINS = tuple(LatencyBin(lo, hi) for lo, hi in zip(_EDGES[:-1], _EDGES[1:], strict=True))
_REASONS: dict[Status, frozenset[str]] = {
    "missing": frozenset({"histogram_input_not_supplied"}),
    "unknown": frozenset(
        {
            "unverified_scrape_coverage_or_series",
            "generation_boundary_without_covered_window",
            "histogram_not_observed",
        }
    ),
    "rejected": frozenset({"histogram_counter_invariant", "incompatible_scope_or_bucket_schema"}),
}


def _exact_bounds(buckets: tuple[Bucket, ...]) -> bool:
    if len(buckets) != len(BOUNDS_SECONDS):
        return False
    for bucket, expected in zip(buckets, BOUNDS_SECONDS, strict=True):
        bound = bucket.upper_bound_seconds
        if expected == "+Inf":
            if type(bound) is not str or bound != expected:
                return False
        elif type(bound) not in (float, int) or bound != expected:
            return False
    return True


def _disjoint_counts(value: HistogramWindow) -> tuple[int, ...] | None:
    if type(value.count) is not int or value.count < 0:
        return None
    total = value.sum_seconds
    if type(total) is int:
        valid_sum = total >= 0
    else:
        valid_sum = type(total) is float and isfinite(total) and total >= 0
    if not valid_sum or (value.count == 0 and total != 0):
        return None
    previous = 0
    counts = []
    for bucket in value.buckets:
        current = bucket.cumulative_count
        if type(current) is not int or current < previous:
            return None
        counts.append(current - previous)
        previous = current
    return tuple(counts) if previous == value.count else None


def _project_row(
    window: Window, result: HistogramResult | None, track: HistogramTrack | None
) -> HeatmapRow:
    if result is None:
        return HeatmapRow(window, "missing", reason_code="histogram_input_not_supplied")
    evidence, value = result.evidence, result.value

    def refuse(status: Status, code: str) -> HeatmapRow:
        return HeatmapRow(window, status, evidence, reason_code=code)

    incompatible = "incompatible_scope_or_bucket_schema"
    unproved = "unverified_scrape_coverage_or_series"
    if result.status != "observed":
        reasons = _REASONS.get(result.status, ())
        if value is not None:
            return refuse("rejected", incompatible)
        if result.reason is None or result.reason.code not in reasons:
            return refuse("unknown", unproved)
        return refuse(result.status, result.reason.code)
    if value is None or value.count is None or value.sum_seconds is None:
        return refuse("unknown", "histogram_not_observed")
    if evidence.coverage.state != "complete" or track is None or evidence.source is None:
        return refuse("unknown", unproved)
    filters = dict(track.scope.filters)
    if (
        selection_relation(evidence, track.scope) != SelectionRelation.MATCHED
        or evidence.window != window
        or value.window != window
        or (evidence.measure, evidence.unit) != (COUNT_MEASURE, COUNT_UNIT)
        or not filters.get("model")
        or not (value.source_target == track.source_target == filters.get("target"))
        or value.series_generation.canonical_replica != filters.get("replica")
        or not _exact_bounds(value.buckets)
    ):
        return refuse("rejected", incompatible)
    series = value.series_generation
    if any(
        not isinstance(key, str) or not key.strip()
        for key in (getattr(series, field.name) for field in fields(series))
    ):
        return refuse("unknown", unproved)
    if (
        series != track.series_generation
        or evidence.source.generation != track.publication_generation
    ):
        return refuse("unknown", "generation_boundary_without_covered_window")
    counts = _disjoint_counts(value)
    if counts is None:
        return refuse("rejected", "histogram_counter_invariant")
    return HeatmapRow(window, "observed", evidence, counts, value.count)


def project_latency_heatmap(
    windows: tuple[tuple[Window, HistogramResult | None], ...], *, track: HistogramTrack | None
) -> LatencyHeatmap:
    """Use only explicitly selected windows; pass None to show a window without input.

    Separately proved subwindows can be selected directly. This is not a
    tiler: it neither splits an incomplete window nor spans gaps. Select a new
    track for an independently proved post-reset series.
    """
    rows = []
    previous_end = None
    for window, result in windows:
        if not window.half_open or (previous_end is not None and window.start < previous_end):
            raise ValueError("select ordered, non-overlapping half-open latency windows")
        rows.append(_project_row(window, result, track))
        previous_end = window.end
    peak = max((count for row in rows for count in (row.counts or ())), default=0)
    return LatencyHeatmap(_BINS, tuple(rows), peak)
