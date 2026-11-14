"""Points (pace-option-draft §4.2): weights, bucket law, reservation bound, fail-open rows.

Pure tests: no server, no clock, no I/O.
"""

from __future__ import annotations

import math

import pytest

from anthropic_throttle_proxy import points

# (model, case, usage in the gateway's vocabulary, HTTP status, burn_watch.points(row)[3]).
# Values were produced by scripts/token-usage-lake/science/burnwatch/burn_watch.py points()
# on the same rows in the ledger's vocabulary (cacheRead/cacheWrite), 14/11/2026. The live
# comparison against the vault script runs by hand (RESULT.md); CI has no vault.
_ROWS = [
    ("claude-fable-5", "cache read", (2000, 800, 300000, 0), 200, 0.012959999999999998),
    ("claude-fable-5", "cache write", (500, 1200, 0, 60000), 200, 0.07823999999999999),
    ("claude-fable-5", "output", (100, 9000, 40000, 0), 200, 0.044256),
    ("claude-fable-5", "aborted summary", (90000, 0, 0, 0), 499, 0.07992),
    ("claude-opus-4-8", "cache read", (1500, 600, 200000, 0), 200, 0.002646),
    ("claude-opus-4-8", "cache write", (300, 900, 0, 45000), 200, 0.011477399999999999),
    ("claude-opus-4-8", "output", (80, 12000, 30000, 0), 200, 0.011565039999999999),
    ("claude-opus-4-8", "aborted summary", (70000, 0, 0, 0), 499, 0.031080000000000007),
    ("claude-sonnet-5", "cache read", (900, 400, 150000, 0), 200, 0.0016226),
    ("claude-sonnet-5", "aborted summary", (50000, 0, 0, 0), 499, 0.011100000000000002),
    ("claude-haiku-5-5", "cache write", (200, 300, 0, 8000), 200, 0.0005499),
    ("claude-haiku-5-5", "completed summary", (30000, 500, 0, 0), 200, 0.0015274999999999998),
]
GOLDEN = [
    (m, c, dict(zip(("input", "output", "cache_read", "cache_creation"), u, strict=True)), st, v)
    for m, c, u, st, v in _ROWS
]


@pytest.mark.parametrize(("model", "case", "usage", "status", "expected"), GOLDEN)
def test_settled_points_matches_the_burn_watch_golden_table(model, case, usage, status, expected):
    assert points.settled_points(model, usage, status) == pytest.approx(expected, rel=1e-12), case


def test_unknown_model_is_unpriced_not_free():
    assert points.settled_points("glm-5.3", {"input": 1000, "output": 1000}) is None
    assert points.settled_points(None, {"input": 1}) is None


def test_a_completed_summary_is_not_priced_at_the_aborted_weight():
    usage = {"input": 90000, "output": 0, "cache_read": 0, "cache_creation": 0}
    aborted = points.settled_points("claude-fable-5", usage, 499)
    completed = points.settled_points("claude-fable-5", usage, 200)
    assert aborted < completed  # fable weight 12e-4 > l1 weight 11.1e-4


@pytest.mark.parametrize(
    ("raw", "mode"),
    [(None, "off"), ("", "off"), ("observe", "observe"), (" Observe ", "observe"),
     ("enforce", "off"), ("bogus", "off")],
)  # fmt: skip
def test_parse_mode_is_off_observe_only(raw, mode):
    assert points.parse_mode(raw) == mode


# --- the bucket law --------------------------------------------------------------------


def test_a_new_bucket_starts_full_and_refill_is_clamped_to_burst():
    bucket = points.Bucket(burst=1.0)
    assert bucket.credit == 1.0
    bucket.refill(1000.0, 0.5)  # anchors
    bucket.debit(0.6)
    bucket.refill(1000.0 + 3600.0, 0.5)  # +0.5
    assert bucket.credit == pytest.approx(0.9)
    bucket.refill(1000.0 + 20 * 3600.0, 0.5)  # would be +10, clamps
    assert bucket.credit == 1.0


def test_debit_can_go_below_zero_and_wait_is_deficit_over_rate():
    bucket = points.Bucket(burst=1.0)
    bucket.debit(9.0)  # the 9-point burst of §2.2
    assert bucket.credit == pytest.approx(-8.0)
    assert bucket.wait_s(0.63) == pytest.approx(8.0 / 0.63 * 3600.0)
    assert bucket.wait_s(0.63) / 3600.0 == pytest.approx(
        12.7, abs=0.1
    )  # "about 14 h at A" ± weights
    assert points.Bucket().wait_s(0.63) == 0.0  # in credit: no wait


def test_the_floor_keeps_the_refill_above_zero():
    bucket = points.Bucket(burst=1.0)
    bucket.debit(1.5)  # credit -0.5
    assert bucket.wait_s(0.0, floor_per_h=0.05) == pytest.approx(0.5 / 0.05 * 3600.0)
    assert math.isfinite(bucket.wait_s(0.0))


def test_a_negative_debit_never_adds_credit():
    bucket = points.Bucket(burst=1.0)
    bucket.debit(-5.0)
    assert bucket.credit == 1.0


@pytest.mark.parametrize("jump", [-7200.0, 7200.0])
def test_a_clock_jump_of_two_hours_never_makes_negative_time_or_exceeds_burst(jump):
    bucket = points.Bucket(burst=1.0)
    bucket.refill(10_000.0, 0.63)
    bucket.debit(0.5)
    bucket.refill(10_000.0 + jump, 0.63)
    assert 0.5 <= bucket.credit <= 1.0
    if jump < 0:
        assert bucket.credit == pytest.approx(0.5)  # no refill from backwards time
        assert bucket.last_refill == 10_000.0 + jump  # re-anchored


def test_no_rate_means_no_refill_and_no_anchor_move():
    bucket = points.Bucket(burst=1.0)
    bucket.refill(0.0, 0.5)
    bucket.debit(0.5)
    bucket.refill(7200.0, None)  # meter unknown: nothing happens
    assert bucket.credit == pytest.approx(0.5) and bucket.last_refill == 0.0
    bucket.refill(7200.0, 0.5)  # the later rate covers the whole gap
    assert bucket.credit == pytest.approx(1.0)


# --- reservation ---------------------------------------------------------------------


@pytest.mark.parametrize("family", ["fable", "opus", "sonnet", "haiku"])
def test_24_concurrent_admits_are_bounded_by_inflight_times_p90(family):
    """Credit 0.1: admit while credit − Σ reservations ≥ 0, so the overshoot is < one p90."""
    p90 = points.P90[family]
    bucket = points.Bucket(burst=1.0, credit=0.1)
    admitted = sum(bucket.reserve(p90) for _ in range(24))
    assert admitted == min(24, math.floor(0.1 / p90) + 1)
    assert bucket.reserved == pytest.approx(admitted * p90)
    assert bucket.reserved <= 0.1 + p90  # the bound the design promises
    for _ in range(admitted):
        bucket.release(p90)
    assert bucket.reserved == 0.0
    bucket.release(p90)  # an extra release never goes negative
    assert bucket.reserved == 0.0


def test_a_fable_reservation_refuses_the_second_request_at_credit_0_1():
    bucket = points.Bucket(burst=1.0, credit=0.1)
    assert bucket.reserve(points.P90["fable"]) is True
    assert bucket.reserve(points.P90["fable"]) is False  # 0.1 − 0.17 < 0


# --- rate, fail-open rows --------------------------------------------------------------


def test_regime_a_spreads_the_remaining_budget_over_the_hours_left():
    now = 1_000_000.0
    reset = now + 163.5 * 3600.0
    # the §2.1 example: gmail at 23:15 with 0.485 of the week free-ish
    assert points.regime_rate(0.2, reset, now) == pytest.approx(80.0 / 163.5)
    assert points.regime_rate(1.0, reset, now) == points.DEFAULT_FLOOR_PER_H  # the trickle


@pytest.mark.parametrize(
    ("util", "reset_delta"),
    [(None, 3600.0), (0.2, None), (0.2, -1.0), (0.2, 0.0), (float("nan"), 3600.0)],
)
def test_no_meter_or_a_past_reset_gives_no_rate_so_no_verdict(util, reset_delta):
    now = 1_000_000.0
    reset = None if reset_delta is None else now + reset_delta
    assert points.regime_rate(util, reset, now) is None


def test_only_regime_a_exists_in_this_step():
    with pytest.raises(ValueError):
        points.regime_rate(0.1, 10_000.0, 0.0, regime="B")


def test_admit_is_unjudged_without_a_rate_or_a_priced_model():
    book = points.Book()
    assert book.admit({"a": None}, 0.0, 0.1) == (None, None, 0.0)  # no meter
    assert book.admit({"a": 0.6}, 0.0, None) == (None, None, 0.0)  # unpriced model
    assert book.admit({}, 0.0, 0.1) == (None, None, 0.0)  # no eligible account


def test_admit_reserves_on_the_richest_account_and_defers_only_when_all_are_in_debt():
    book = points.Book()
    book.bucket("poor").credit = -2.0
    book.bucket("rich").credit = 0.5
    verdict, bid, held = book.admit({"poor": 0.6, "rich": 0.6}, 0.0, 0.1)
    assert (verdict, bid, held) == (False, "rich", 0.1)
    book.release(bid, held)
    book.bucket("rich").credit = -1.0
    assert book.admit({"poor": 0.6, "rich": 0.6}, 0.0, 0.1) == (True, None, 0.0)
    assert book.bucket("poor").reserved == 0.0 == book.bucket("rich").reserved


def test_verdict_transitions_are_reported_once_each():
    book = points.Book()
    assert [book.note_verdict(v) for v in (None, False, True, True, False, False, True)] == [
        False, False, True, False, True, False, True,
    ]  # fmt: skip
    assert book.would_defer_total == {"all_in_debt": 2}


def test_debit_tracks_since_reset_and_restarts_when_the_week_resets():
    book = points.Book()
    kw = {"rate": 0.5, "model": "m", "project": "p"}
    book.debit("a", 0.2, 0.0, reset=100.0, **kw)
    book.debit("a", 0.3, 1.0, reset=100.0, **kw)
    assert book.settled_since_reset("a", 100.0) == pytest.approx(0.5)
    book.debit("a", 0.1, 2.0, reset=900.0, **kw)  # a new week
    assert book.settled_since_reset("a", 900.0) == pytest.approx(0.1)
    assert book.settled_since_reset("a", 100.0) == 0.0


def test_the_sixty_minute_window_forgets_older_debits():
    book = points.Book()
    kw = {"rate": None, "reset": None, "project": "p"}
    book.debit("a", 1.0, 0.0, model="old", **kw)
    book.debit("a", 2.0, 4000.0, model="new", **kw)
    assert book.view({}, 4000.0)["settled_60m_by_model"] == {"new": 2.0}


def test_unpriced_is_logged_once_per_model_but_counted_every_time():
    book = points.Book()
    assert [book.note_unpriced("glm"), book.note_unpriced("glm"), book.note_unpriced("x")] == [
        True, False, True,
    ]  # fmt: skip
    assert book.unpriced_total == 3
