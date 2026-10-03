"""A headerless 429 on a PLAN lane is concurrency, not a budget wall.

Measured 23/09/2026 on the MiMo Token Plan lane: every upstream 429 arrives
without ``Retry-After`` and without any budget header of its own, while the plan
sat at ~6 % of an 82 B-credit month. The default classification ("no evidence ⇒
budget") turned each of those into a 30 s synthetic hold and collapsed the lane
to a single slot, so a concurrency blip became a queue collapse — 156 local
queue-wait timeouts in 24 h on the same lane.

These tests pin the disambiguation. With the validated ``METER_BINDINGS``
account binding (spec 279 seat-b plan) the read is EXACT-bearer: only the
bearer's own bound row may clear it — exhausted A must not drag fresh B into
budget backoff, and fresh B must never clear A's wall. With the binding
unconfigured, a fresh plan meter below the pressure line outranks the
headerless default only while EVERY same-provider allowance in the report
(``mimo:plan`` + its Team sibling, spec 281) shows fresh headroom. Every
unknown state — unset knob, stale report, unreadable meter, wrong lane,
unknown/unmapped/malformed binding — falls back to the conservative budget
backoff.
"""

import time

import pytest

from anthropic_throttle_proxy import accounts, config, lanes, proxy

PLAN_LANE = "mimo:plan"


@pytest.fixture(autouse=True)
def _plan_knobs_default_off(monkeypatch):
    """Every test starts from the shipped default: no plan lane, no bindings."""
    monkeypatch.setattr(config, "PLAN_METER_LANE", "")
    monkeypatch.setattr(config, "PLAN_PRESSURE_PERCENT", 80.0)
    monkeypatch.setattr(config, "METER_BINDINGS", {})
    monkeypatch.setattr(config, "METER_BINDINGS_VALID", True)
    monkeypatch.setattr(config, "METER_BINDING_REQUIRED", False)
    yield


TEAM_LANE = "mimo:team-owner"


def _snapshot(*, lane_status: str = "ok", used: float | None = 6.3, lane_id: str = PLAN_LANE):
    meters = [] if used is None else [{"label": "monthly", "used_pct": used}]
    return {"lanes": [{"id": lane_id, "status": lane_status, "meters": meters}]}


def _sibling_snapshot(
    *,
    plan_used=6.3,
    plan_status="ok",
    team_used=100.0,
    team_status="ok",
    team_id=TEAM_LANE,
    extra=(),
):
    """One instance, TWO independent allowances (spec 281): plan + Team seat."""

    def row(lane_id, status, used, label):
        meters = [] if used is None else [{"label": label, "used_pct": used}]
        return {"id": lane_id, "kind": "mimo", "status": status, "meters": meters}

    return {
        "lanes": [
            row(PLAN_LANE, plan_status, plan_used, "monthly"),
            row(team_id, team_status, team_used, "seat"),
            *extra,
        ]
    }


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


def test_sibling_lane_wall_is_not_rescued_by_plan_headroom(monkeypatch):
    """Hypothesis (283 routing verification), reproduced here first.

    ``THROTTLE_PLAN_METER_LANE`` names ONE lane's meter, but the fallback
    classifies a headerless 429 for EVERY bearer on the instance. Since spec
    281 one instance routes SIBLING allowances (``mimo:plan`` +
    ``mimo:team-owner`` — two independent allowances, never additive), so an
    exhausted Team seat whose quota is spent must NOT read as "concurrency"
    just because the individual plan is at 6 %. That misclassification blocks
    real pushback: no AIMD shrink, a 2 s cooldown and a doomed keepalive hold
    for a bearer that is at its own budget wall.
    """
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(lanes, "view", lambda now: _sibling_snapshot())
    assert proxy._budget_under_pressure({}, "team-seat") is True
    pause, synthetic = proxy._pushback_pause({}, "team-seat")
    assert synthetic is True
    assert pause == min(max(0.0, config.AIMD_BACKOFF_S), config.MAX_HOLD_RETRY_AFTER_S)


def test_sibling_lane_headroom_keeps_the_concurrency_inference(monkeypatch):
    """Both allowances far from their walls: whichever a bearer spends, its
    headerless 429 cannot be a budget wall — the 23/09 inference stands."""
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(lanes, "view", lambda now: _sibling_snapshot(team_used=6.0))
    assert proxy._plan_lane_has_headroom() is True
    assert proxy._budget_under_pressure({}, "team-seat") is False
    pause, synthetic = proxy._pushback_pause({}, "team-seat")
    assert synthetic is True
    assert pause == max(0.0, config.CONCURRENCY_COOLDOWN_S)


def test_unreadable_sibling_lane_falls_back_to_budget(monkeypatch):
    """UNKNOWN IS NOT HEADROOM, and it now applies per allowance in play: a
    sibling the probe could not read (or a purchased-but-unassigned seat) may
    be exactly the quota this bearer is spending."""
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(
        lanes,
        "view",
        lambda now: _sibling_snapshot(team_used=None, team_status="unknown"),
    )
    assert proxy._plan_lane_has_headroom() is False
    assert proxy._budget_under_pressure({}, "team-seat") is True


def test_other_provider_rows_do_not_speak_for_a_plan_bearer(monkeypatch):
    """Only same-provider allowances are candidates for what a plan-lane bearer
    spends; an exhausted unrelated lane must not disable the inference."""
    monkeypatch.setattr(config, "PLAN_METER_LANE", PLAN_LANE)
    monkeypatch.setattr(
        lanes,
        "view",
        lambda now: _sibling_snapshot(
            team_used=6.0,
            extra=(
                {
                    "id": "zai:glm",
                    "kind": "zai",
                    "status": "exhausted",
                    "meters": [{"label": "5h", "used_pct": 100.0}],
                },
            ),
        ),
    )
    assert proxy._plan_lane_has_headroom() is True
    assert proxy._budget_under_pressure({}, "team-seat") is False


# ── exact-bearer classification (292: bound meter backoff) ─────────────────
#
# With the validated METER_BINDINGS account binding (spec 279 seat-b plan), a
# headerless 429 is classified by THIS bearer's own bound meter row — never a
# sibling's. Exhausted A must not drag independently-bound fresh B into budget
# backoff, and fresh B must never clear A's wall.


def _bind(monkeypatch, bindings, *, required=False, valid=True):
    monkeypatch.setattr(config, "METER_BINDINGS", bindings)
    monkeypatch.setattr(config, "METER_BINDINGS_VALID", valid)
    monkeypatch.setattr(config, "METER_BINDING_REQUIRED", required)
    monkeypatch.setattr(
        accounts,
        "bearer_labels",
        lambda: {"bid-a": "plan", "bid-b": "team-b", "bid-x": "ghost"},
    )


def _bound_rows(*, plan_used=100.0, plan_status="ok", team_used=6.3, team_status="ok"):
    """Two bound rows: the plan seat and the independently-bound team-b seat."""

    return _sibling_snapshot(
        plan_used=plan_used,
        plan_status=plan_status,
        team_used=team_used,
        team_status=team_status,
        team_id="mimo:team-b",
    )


def test_bound_bearer_reads_its_own_meter_not_a_sibling(monkeypatch):
    """Exhausted A must not force budget backoff on fresh B — and B's headroom
    must never clear A's wall (no borrowing in either direction)."""
    _bind(monkeypatch, {"plan": PLAN_LANE, "team-b": "mimo:team-b"})
    monkeypatch.setattr(lanes, "view", lambda now: _bound_rows())
    assert proxy._budget_under_pressure({}, "bid-b") is False  # its own fresh row
    assert proxy._budget_under_pressure({}, "bid-a") is True  # its own spent row
    pause, synthetic = proxy._pushback_pause({}, "bid-b")
    assert synthetic is True
    assert pause == max(0.0, config.CONCURRENCY_COOLDOWN_S)


@pytest.mark.parametrize("team_status", ["stale", "unknown"])
def test_bound_stale_or_unreadable_row_fails_closed(monkeypatch, team_status):
    """B's own row must be FRESH evidence: stale/unknown fails closed even
    while the sibling plan row is wide open — and A's fresh row still clears A."""
    _bind(monkeypatch, {"plan": PLAN_LANE, "team-b": "mimo:team-b"})
    monkeypatch.setattr(
        lanes, "view", lambda now: _bound_rows(plan_used=6.3, team_status=team_status)
    )
    assert proxy._budget_under_pressure({}, "bid-b") is True
    assert proxy._budget_under_pressure({}, "bid-a") is False


def test_unknown_and_malformed_bindings_fail_closed(monkeypatch):
    """Unknown binding, unmapped label and malformed mapping all fail closed —
    a bad entry must never read as headroom or disappear into feature-off."""
    monkeypatch.setattr(lanes, "view", lambda now: _bound_rows(plan_used=6.3, team_used=6.3))
    _bind(monkeypatch, {"plan": PLAN_LANE, "team-b": "mimo:team-b"})
    assert proxy._budget_under_pressure({}, "bid-x") is True  # ghost label: unmapped
    assert proxy._budget_under_pressure({}, "stranger") is True  # no label at all
    _bind(monkeypatch, {"plan": PLAN_LANE}, valid=False)  # whole mapping malformed
    assert proxy._budget_under_pressure({}, "bid-b") is True
    _bind(monkeypatch, {"plan": ""})  # malformed entry residue
    assert proxy._budget_under_pressure({}, "bid-a") is True


def test_explicit_unified_evidence_precedes_the_meter(monkeypatch):
    """Unified response headers and the fresh cache outrank the meter fallback:
    no new budget/fallback — the meter only fills the headerless, cacheless gap."""
    _bind(monkeypatch, {"plan": PLAN_LANE, "team-b": "mimo:team-b"})
    monkeypatch.setattr(lanes, "view", lambda now: _bound_rows(plan_used=6.3, team_used=6.3))
    # Explicit pressure on the RESPONSE is budget even with a fresh meter.
    assert (
        proxy._budget_under_pressure(
            {"anthropic-ratelimit-unified-status": "allowed_warning"}, "bid-b"
        )
        is True
    )
    monkeypatch.setitem(
        config.bearer_state,
        "bid-b",
        {
            "unified": {
                "status_5h": "allowed",
                "util_5h": 0.2,
                "reset_5h": time.time() + 600,
            },
            "unified_at": time.time(),
        },
    )
    # B's own spent meter conflicts with its fresh cached allowed sample.
    monkeypatch.setattr(lanes, "view", lambda now: _bound_rows(team_used=100.0))
    assert proxy._budget_under_pressure({}, "bid-b") is False
