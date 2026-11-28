from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from anthropic_throttle_proxy.ui.observatory_evidence import (
    Coverage,
    Evidence,
    Generation,
    Scope,
    SourceRef,
    Window,
)
from anthropic_throttle_proxy.ui.observatory_latency import (
    COUNT_MEASURE,
    COUNT_UNIT,
    Bucket,
    HistogramResult,
    HistogramTrack,
    HistogramWindow,
    Reason,
    SeriesGeneration,
    project_latency_heatmap,
)

START = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)
OPENING = Window(START, START + timedelta(seconds=10), half_open=True)
NEXT = Window(OPENING.end, OPENING.end + timedelta(seconds=15), half_open=True)
BOUNDS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25, 60, 120, 300, 600, "+Inf")
# Mathematically known P7 oracle example: 3 finite observations, 2 above 600 s.
CUMULATIVE = (1, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 5)
DISJOINT = (1, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 2)
SERIES = SeriesGeneration("collector", "config", "classic-v1", "emitter", "replica-a", "reset-a")
GENERATION = Generation("test-authority", "test-publication", "a" * 64)
SCOPE = Scope(
    "forwarded_requests",
    (("model", "test-model"), ("target", "target-a"), ("replica", "replica-a")),
    "test-namespace",
)
TRACK = HistogramTrack(SCOPE, "target-a", SERIES, GENERATION)


def observed(window=OPENING, *, track=TRACK, counts=CUMULATIVE, count=5, sum_seconds=1301.0):
    source = SourceRef("histogram", "b" * 64, track.publication_generation)
    proof = SourceRef("scrape_coverage", "c" * 64, track.publication_generation)
    return HistogramResult(
        status="observed",
        value=HistogramWindow(
            track.source_target,
            track.series_generation,
            window,
            tuple(Bucket(bound, n) for bound, n in zip(BOUNDS, counts, strict=True)),
            count,
            sum_seconds,
        ),
        evidence=Evidence(
            source,
            window,
            track.scope,
            COUNT_MEASURE,
            COUNT_UNIT,
            Coverage("complete", refs=(proof,)),
        ),
    )


def render(heatmap):
    root = Path(__file__).parents[1] / "src/anthropic_throttle_proxy/ui/templates"
    env = Environment(
        loader=FileSystemLoader(root),
        autoescape=select_autoescape(),
        undefined=StrictUndefined,
    )
    return env.get_template("partials/observatory_latency.html").render(latency_heatmap=heatmap)


def test_disjoint_counts_conserve_each_window_with_open_overflow_and_exact_edges():
    first = observed()
    second = observed(NEXT, counts=(0,) * 12 + (1,), count=1, sum_seconds=601.0)
    heatmap = project_latency_heatmap(((OPENING, first), (NEXT, second)), track=TRACK)
    a, b = heatmap.rows
    assert (a.window, a.status, a.counts, a.count) == (OPENING, "observed", DISJOINT, 5)
    assert (b.window, b.status, b.counts, b.count) == (NEXT, "observed", (0,) * 12 + (1,), 1)
    assert a.evidence is first.evidence
    assert sum(a.counts) == a.count
    assert sum(b.counts) == b.count
    assert [(bin_.lower_seconds, bin_.upper_seconds) for bin_ in heatmap.bins][::6] == [
        (None, 0.1),
        (5, 10),
        (600, None),
    ]
    assert heatmap.peak_count == 2
    html = render(heatmap)
    assert 'scope="row"' in html and 'scope="col"' in html
    assert 'datetime="2026-01-01T12:00:00.123456+00:00"' in html
    assert "&gt; 600 s" in html
    assert "1301" not in html  # Do not present the independent sum as an observation count.
    assert "test-namespace" not in html and "test-publication" not in html


def test_measured_zero_and_absent_window_are_rendered_differently():
    zero = observed(counts=(0,) * len(BOUNDS), count=0, sum_seconds=0.0)
    heatmap = project_latency_heatmap(((OPENING, zero), (NEXT, None)), track=TRACK)
    measured, missing = heatmap.rows
    assert measured.count == 0 and measured.counts == (0,) * len(BOUNDS)
    assert missing.status == "missing" and missing.count is None and missing.counts is None
    assert heatmap.peak_count == 0
    html = render(heatmap)
    assert html.count('class="latency-count">0</span>') == len(BOUNDS)
    assert "Unavailable" in html and "No histogram was supplied" in html
    assert "<meter" not in html  # No division or phantom intensity when all measured counts are 0.


@pytest.mark.parametrize("state", ["partial", "sampled", "unknown"])
def test_never_turns_incomplete_larger_window_into_selected_total(state):
    reading = observed()
    other_scope = replace(reading.evidence, coverage=Coverage(state, ("unproved_scrapes",)))
    interrupted = replace(reading, evidence=other_scope)
    smaller = Window(START, START + timedelta(seconds=4), half_open=True)
    separately_proved = observed(smaller)
    result = project_latency_heatmap(((OPENING, interrupted),), track=TRACK).rows[0]
    assert result.status == "unknown" and result.counts is None and result.count is None
    assert result.evidence is other_scope
    assert project_latency_heatmap(((smaller, separately_proved),), track=TRACK).rows[0].count == 5
    wrong_window = project_latency_heatmap(((OPENING, separately_proved),), track=TRACK).rows[0]
    assert wrong_window.status == "rejected" and wrong_window.counts is None


@pytest.mark.parametrize(
    "change",
    [
        {"count": None},
        {"sum_seconds": None},
        {"count": 4},
        {"count": True},
        {"sum_seconds": float("nan")},
        {"sum_seconds": float("inf")},
        {"sum_seconds": -1.0},
        {"sum_seconds": True},
        {"buckets": tuple(Bucket(bound, 0) for bound in BOUNDS), "count": 0},
    ],
)
def test_independent_count_and_sum_are_required_and_never_replaced(change):
    reading = observed()
    outcome = project_latency_heatmap(
        ((OPENING, replace(reading, value=replace(reading.value, **change))),), track=TRACK
    ).rows[0]
    assert outcome.status in {"unknown", "rejected"}
    assert outcome.counts is None and outcome.count is None


@pytest.mark.parametrize(
    ("index", "bucket"),
    [
        (0, Bucket(True, 1)),
        (3, Bucket(1.1, 3)),
        (12, Bucket(float("inf"), 5)),
        (1, Bucket(0.25, -1)),
        (1, Bucket(0.25, 0)),
        (0, Bucket(0.1, True)),
    ],
)
def test_refuses_bucket_schema_drift_and_nonmonotonic_cumulative_counts(index, bucket):
    reading = observed()
    buckets = list(reading.value.buckets)
    buckets[index] = bucket
    outcome = project_latency_heatmap(
        ((OPENING, replace(reading, value=replace(reading.value, buckets=tuple(buckets)))),),
        track=TRACK,
    ).rows[0]
    assert outcome.status == "rejected" and outcome.counts is None


def test_missing_bucket_and_the_same_edges_with_a_different_grain_are_not_filled():
    reading = observed()
    short = replace(reading, value=replace(reading.value, buckets=reading.value.buckets[:-1]))
    short_outcome = project_latency_heatmap(((OPENING, short),), track=TRACK).rows[0]
    assert short_outcome.status == "rejected" and short_outcome.counts is None
    start = START.replace(microsecond=0)
    end = start + timedelta(seconds=10)
    grained = Window(start, end, grain_seconds=5, half_open=True)
    ungrained = Window(start, end, half_open=True)
    result = project_latency_heatmap(((grained, observed(ungrained)),), track=TRACK).rows[0]
    assert result.status == "rejected" and result.counts is None


@pytest.mark.parametrize(
    "field",
    (
        "collector_generation",
        "scrape_configuration",
        "histogram_schema_revision",
        "canonical_emitter",
        "exporter_reset_segment",
    ),
)
def test_generation_changes_never_get_stitched_even_when_later_count_is_larger(field):
    after = replace(TRACK, series_generation=replace(SERIES, **{field: "other"}))
    later = observed(NEXT, track=after, counts=CUMULATIVE[:-1] + (20,), count=20)
    heatmap = project_latency_heatmap(((OPENING, observed()), (NEXT, later)), track=TRACK)
    assert heatmap.rows[0].count == 5
    assert heatmap.rows[1].status == "unknown" and heatmap.rows[1].counts is None
    assert project_latency_heatmap(((NEXT, later),), track=after).rows[0].count == 20


@pytest.mark.parametrize(
    "changed_track",
    [
        replace(TRACK, source_target="other"),
        replace(TRACK, series_generation=replace(SERIES, canonical_replica="other")),
        replace(TRACK, scope=Scope("forwarded_requests")),
        replace(TRACK, scope=replace(SCOPE, filters=(*SCOPE.filters, ("account", "other")))),
        replace(TRACK, publication_generation=replace(GENERATION, id="other")),
    ],
)
def test_other_target_replica_scope_or_publication_cannot_fill_selected_window(changed_track):
    result = project_latency_heatmap(((OPENING, observed()),), track=changed_track).rows[0]
    assert result.status in {"unknown", "rejected"} and result.counts is None


def test_refuses_measure_swap_unbound_track_and_unsorted_or_overlapping_edges():
    reading = observed()
    wrong_evidence = replace(reading.evidence, measure="latency_sum", unit="seconds")
    result = project_latency_heatmap(
        ((OPENING, replace(reading, evidence=wrong_evidence)),), track=TRACK
    ).rows[0]
    assert result.status == "rejected" and result.counts is None
    assert project_latency_heatmap(((OPENING, reading),), track=None).rows[0].count is None
    for windows in (((NEXT, None), (OPENING, None)), ((OPENING, None), (OPENING, None))):
        with pytest.raises(ValueError, match="non-overlapping"):
            project_latency_heatmap(windows, track=TRACK)


def test_localized_gap_has_no_counts_between_separately_proved_windows():
    gap = Window(OPENING.end, NEXT.start + timedelta(seconds=1), half_open=True)
    after = Window(gap.end, NEXT.end, half_open=True)
    interrupted = observed(gap)
    coverage = Coverage("partial", ("missed_scrape",), interrupted.evidence.coverage.refs, (gap,))
    evidence = replace(interrupted.evidence, coverage=coverage)
    missing = HistogramResult(
        status="unknown",
        value=None,
        evidence=evidence,
        reason=Reason("unverified_scrape_coverage_or_series", "do-not-display-internal-note"),
    )
    heatmap = project_latency_heatmap(
        ((OPENING, observed()), (gap, missing), (after, observed(after))), track=TRACK
    )
    assert [row.count for row in heatmap.rows] == [5, None, 5]
    assert heatmap.rows[1].evidence.coverage.gaps == (gap,)
    html = render(heatmap)
    assert "Continuous scrape coverage or series identity is unproved." in html
    assert "do-not-display-internal-note" not in html
    assert html.count('class="latency-count"') == 2 * len(BOUNDS)


def test_never_claims_empty_selection_is_a_measured_zero():
    assert "No latency windows were selected" in render(project_latency_heatmap((), track=None))
