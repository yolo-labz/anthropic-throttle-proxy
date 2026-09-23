"""A headerless 429 on a PLAN lane is concurrency, not a budget wall.

Measured 23/09/2026 on the MiMo Token Plan lane: every upstream 429 arrives
without ``Retry-After`` and without any budget header of its own, while the plan
sat at ~6 % of an 82 B-credit month. The default classification ("no evidence ⇒
budget") turned each of those into a 30 s synthetic hold and collapsed the lane
to a single slot, so a concurrency blip became a queue collapse — 156 local
queue-wait timeouts in 24 h on the same lane.

These tests pin the disambiguation: a fresh plan meter below the pressure line
outranks the headerless default, and every unknown state (unset knob, stale
report, unreadable meter, wrong lane) falls back to the conservative budget
backoff.
"""

import time

import pytest

from anthropic_throttle_proxy import config, lanes, proxy

PLAN_LANE = "mimo:plan"


@pytest.fixture(autouse=True)
def _plan_knobs_default_off(monkeypatch):
    """Every test starts from the shipped default: no plan lane configured."""
    monkeypatch.setattr(config, "PLAN_METER_LANE", "")
    monkeypatch.setattr(config, "PLAN_PRESSURE_PERCENT", 80.0)
    yield


def _snapshot(*, lane_status: str = "ok", used: float | None = 6.3, lane_id: str = PLAN_LANE):
    meters = [] if used is None else [{"label": "monthly", "used_pct": used}]
    return {"lanes": [{"id": lane_id, "status": lane_status, "meters": meters}]}


def test_unset_knob_keeps_the_conservative_budget_default():
    """No plan lane configured must behave exactly as before this change."""
    assert proxy._budget_under_pressure({}, "") is True
    pause, synthetic = proxy._pushback_pause({}, "")
    assert synthetic is True
    assert pause == min(max(0.0, config.AIMD_BACKOFF_S), config.MAX_HOLD_RETRY_AFTER_S)


def test_fresh_plan_below_pressure_is_concurrency(monkeypatch):
    """6.3 % of a monthly allowance cannot be a budget wall."""
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(lanes, "view", lambda now: _snapshot())
    assert proxy._plan_lane_has_headroom() is True
    assert proxy._budget_under_pressure({}, "") is False
    pause, synthetic = proxy._pushback_pause({}, "")
    assert synthetic is True
    assert pause == max(0.0, config.CONCURRENCY_COOLDOWN_S)


def test_plan_at_or_over_pressure_is_a_budget_wall(monkeypatch):
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(lanes, "view", lambda now: _snapshot(used=92.0))
    assert proxy._plan_lane_has_headroom() is False
    assert proxy._budget_under_pressure({}, "") is True


@pytest.mark.parametrize("lane_status", ["stale", "unknown", "error"])
def test_unknown_plan_states_fall_back_to_budget(monkeypatch, lane_status):
    """UNKNOWN IS NOT HEADROOM: a probe that could not read the plan is a wall."""
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(lanes, "view", lambda now: _snapshot(lane_status=lane_status))
    assert proxy._plan_lane_has_headroom() is False
    assert proxy._budget_under_pressure({}, "") is True


@pytest.mark.parametrize(
    "snapshot",
    [
        {"lanes": []},
        {"lanes": [{"id": "other:plan", "status": "ok", "meters": [{"used_pct": 1.0}]}]},
        _snapshot(used=None),
        {"lanes": "not-a-list"},
    ],
)
def test_missing_lane_or_meter_is_not_headroom(monkeypatch, snapshot):
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(lanes, "view", lambda now: snapshot)
    assert proxy._plan_lane_has_headroom() is False
    assert proxy._budget_under_pressure({}, "") is True


def test_a_real_retry_after_still_wins_over_the_plan_meter(monkeypatch):
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(lanes, "view", lambda now: _snapshot())
    pause, synthetic = proxy._pushback_pause({"retry-after": "9000"}, "")
    assert pause == 9000.0
    assert synthetic is False


def test_plan_meter_used_percent_reads_the_fullest_meter(monkeypatch):
    snapshot = {
        "lanes": [
            {
                "id": PLAN_LANE,
                "status": "ok",
                "meters": [{"used_pct": 6.3}, {"used_pct": 41.0}, {"used_pct": None}],
            }
        ]
    }
    monkeypatch.setattr(lanes, "view", lambda now: snapshot)
    assert lanes.plan_meter_used_percent(PLAN_LANE, time.time()) == 41.0
    assert lanes.plan_meter_used_percent("", time.time()) is None
    assert lanes.plan_meter_used_percent("nope", time.time()) is None
