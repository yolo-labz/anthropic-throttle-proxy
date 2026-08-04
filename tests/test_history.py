"""History ring + the header signals derived from it."""

from __future__ import annotations

import pytest

from anthropic_throttle_proxy import history
from anthropic_throttle_proxy.ui import routes, signals


@pytest.fixture(autouse=True)
def _clean_history():
    history.reset()
    yield
    history.reset()


def test_observe_splits_served_from_pushback():
    history.observe(200, 0.4)
    history.observe(429, 0.1)
    history.observe(529, 0.2)
    point = history.record(queued=3, inflight=2, cap=8, now=1000.0)
    assert (point.served, point.errors) == (3, 2)
    assert (point.queued, point.inflight, point.cap) == (3, 2, 8)


def test_record_closes_the_bucket():
    history.observe(200, 1.0)
    history.record(queued=0, inflight=0, cap=1, now=1000.0)
    second = history.record(queued=0, inflight=0, cap=1, now=1010.0)
    assert second.served == 0 and second.p95 is None
    assert [p.t for p in history.series()] == [1000.0, 1010.0]


def test_ring_drops_the_oldest_point_past_the_window():
    for i in range(history.POINTS + 5):
        history.record(queued=0, inflight=0, cap=1, now=float(i))
    series = history.series()
    assert len(series) == history.POINTS
    assert series[0].t == 5.0


def test_quantiles_track_the_slow_tail():
    for d in (0.1, 0.2, 0.3, 5.0):
        history.observe(200, d)
    point = history.record(queued=0, inflight=0, cap=1, now=1000.0)
    assert point.p50 == pytest.approx(0.2)
    assert point.p95 == pytest.approx(5.0)


def test_level_since_holds_the_transition_instant():
    assert history.level_since("healthy", now=100.0) == 0.0
    # Same level, later render — the clock runs from the TRANSITION, not from
    # the last call, or the strip would read "just now" forever.
    assert history.level_since("healthy", now=160.0) == 60.0
    assert history.level_since("throttled", now=170.0) == 0.0


def test_sparkline_zero_baseline_and_peak():
    spark = signals.sparkline([0.0, 5.0, 10.0], width=100.0, height=20.0)
    assert spark.peak == 10.0
    # Zero sits on the baseline, the peak on the top edge — a flat-but-high
    # trace must read as high, which min-anchored scaling would hide.
    assert spark.points == "0.0,20.0 50.0,10.0 100.0,0.0"


def test_sparkline_survives_no_history():
    assert signals.sparkline([]).points == ""
    assert signals.sparkline([0.0, 0.0]).points == "0.0,20.0 96.0,20.0"


def test_collect_reports_rate_errors_and_saturation():
    for _ in range(6):
        for _ in range(10):
            history.observe(200, 0.5)
        history.observe(429, 0.5)
        history.record(queued=4, inflight=4, cap=8, now=1000.0)
    by_key = {s.key: s for s in signals.collect()}
    assert set(by_key) == {"rate", "errors", "latency", "saturation"}
    assert by_key["rate"].value == "66"  # 11 per 10 s bucket → 66/min
    assert by_key["errors"].value == "6.0"
    assert by_key["errors"].level == "crit"
    assert by_key["saturation"].value == "100%"  # (4 + 4) of a cap of 8
    assert by_key["latency"].value == "500ms"


def test_status_carries_how_long_the_verdict_has_held():
    history.level_since("healthy", now=500.0)
    status = routes._compute_status(
        [{"bearer_id": "b1", "unified": None, "limiter": None, "queued": 0}],
        queue_mode="fair",
        now=1220.0,
    )
    assert status["verdict"] == "HEALTHY"
    assert status["since"] == "12m"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "just now"), (59, "just now"), (60, "1m"), (3599, "59m"), (7500, "2h 05m")],
)
def test_fmt_since(seconds, expected):
    assert routes._fmt_since(seconds) == expected
