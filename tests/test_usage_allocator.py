"""Spec 377 phase 1: weekly pacing and client-share rules, offline and pure."""

from __future__ import annotations

import math

import pytest

from anthropic_throttle_proxy import usage_allocator as ua

NOW = 1_000_000.0
WEEK = ua.WEEK_S
BURST = ua.DEFAULT_BURST


def _states(util_by_label: dict[str, float], elapsed: float, burst: float = BURST):
    reset = NOW + (1.0 - elapsed) * WEEK
    return [ua.account_state(k, v, reset, NOW, burst) for k, v in util_by_label.items()], reset


def test_default_burst_is_one_six_hour_slice_of_the_week() -> None:
    assert ua.DEFAULT_BURST == pytest.approx(1 / 28)
    assert ua.parse_burst("garbage") == ua.DEFAULT_BURST
    assert ua.parse_burst("inf") == ua.DEFAULT_BURST
    assert ua.parse_burst("nan") == ua.DEFAULT_BURST
    assert ua.parse_burst("1.5") == ua.DEFAULT_BURST
    assert ua.parse_burst("0.1") == pytest.approx(0.1)


def test_mode_parsing_defaults_to_off() -> None:
    assert ua.parse_mode(None) == "off"
    assert ua.parse_mode("ENFORCE") == "enforce"
    assert ua.parse_mode("bogus") == "off"


def test_shares_parse_drops_bad_entries() -> None:
    assert ua.parse_shares("side-projects:0.4,home:0.3,bad,x:inf,:0.2,y:0") == {
        "side-projects": 0.4,
        "home": 0.3,
    }


# (1) Replay of the 31/10 13:35 pace: gmail ~1.20x, proton ~0.94x of the elapsed week.
def test_enforce_never_dispatches_past_allowed_while_other_account_has_room() -> None:
    elapsed = 0.5
    util = {"gmail": 1.20 * elapsed, "proton": 0.94 * elapsed}
    states, reset = _states(util, elapsed)
    allowed = ua.allowed_7d(NOW, reset, BURST)
    assert states[0]["state"] == "ahead"  # gmail starts over the line
    assert states[1]["state"] == "on_schedule"

    dispatched = {"gmail": 0, "proton": 0}
    deferred = 0
    current = dict(util)
    for _ in range(100):
        current_states = [ua.account_state(k, v, reset, NOW, BURST) for k, v in current.items()]
        verdict, detail = ua.decide_pacing(
            current_states, mode="enforce", priority=False, now=NOW, cap_s=1e9
        )
        if verdict == "defer":
            deferred += 1
            continue
        pool = detail if detail is not None else list(current)
        chosen = min(pool, key=lambda k: current[k])
        # Never dispatch to an account already past its allowance.
        assert current[chosen] <= allowed + 1e-12, (chosen, current[chosen], allowed)
        dispatched[chosen] += 1
        current[chosen] += 0.002

    assert dispatched["gmail"] == 0  # gmail is ahead from the start, so it is never used
    assert dispatched["proton"] > 0
    assert deferred > 0  # once proton reaches the line, non-priority calls are deferred


# (2) All accounts ahead: non-priority deferred with catch-up Retry-After; priority passes.
def test_all_ahead_defers_non_priority_with_catch_up_retry_after() -> None:
    elapsed = 0.5
    util = {"gmail": 0.70, "proton": 0.60}
    states, reset = _states(util, elapsed)
    verdict, retry = ua.decide_pacing(states, mode="enforce", priority=False, now=NOW, cap_s=1e9)
    assert verdict == "defer"
    expected = min(ua.catchup_wait_s(v, reset, NOW, BURST) for v in util.values())
    assert abs(retry - expected) <= 1.0
    # Hand-computed on the decaying line: e* = (0.60 - b)/(1 - b); catch-up = (e* - 0.5)·W.
    e_star = (0.60 - BURST) / (1.0 - BURST)
    assert retry == pytest.approx((e_star - 0.5) * WEEK, abs=2.0)

    verdict, detail = ua.decide_pacing(states, mode="enforce", priority=True, now=NOW, cap_s=1e9)
    assert verdict == "route" and detail is None


def test_retry_after_is_capped_by_the_queue_retry_ceiling() -> None:
    states, _ = _states({"gmail": 0.70, "proton": 0.60}, 0.5)
    verdict, retry = ua.decide_pacing(states, mode="enforce", priority=False, now=NOW, cap_s=300)
    assert verdict == "defer" and retry == 300


# (3) Observe (and off) never change a routing decision.
@pytest.mark.parametrize("elapsed", [0.1, 0.5, 0.9])
@pytest.mark.parametrize("util", [(0.02, 0.9), (0.9, 0.9), (0.2, 0.2)])
def test_observe_is_byte_identical_to_off(elapsed: float, util) -> None:
    states, _ = _states({"a": util[0], "b": util[1]}, elapsed)
    off = ua.decide_pacing(states, mode="off", priority=False, now=NOW, cap_s=300)
    observe = ua.decide_pacing(states, mode="observe", priority=False, now=NOW, cap_s=300)
    assert off == observe == ("route", None)


# (4) Client share: defer only when over share AND fleet over schedule (measured tokens only).
def test_client_over_share_is_deferred_only_when_fleet_over_schedule() -> None:
    shares = {"side-projects": 0.4, "home": 0.3}
    tokens = {"side-projects": 900, "home": 100}  # 90% of measured tokens
    assert ua.decide_client_share("side-projects", tokens, shares, fleet_ahead=True) == "defer"
    assert ua.decide_client_share("side-projects", tokens, shares, fleet_ahead=False) == "admit"
    assert ua.decide_client_share("home", tokens, shares, fleet_ahead=True) == "admit"
    assert ua.decide_client_share("unlisted", tokens, shares, fleet_ahead=True) == "admit"


def test_unmeasured_usage_is_never_a_reason_to_defer() -> None:
    shares = {"side-projects": 0.4}
    assert ua.decide_client_share("side-projects", {}, shares, fleet_ahead=True) == "admit"
    assert (
        ua.decide_client_share("side-projects", {"side-projects": 0}, shares, fleet_ahead=True)
        == "admit"
    )


def test_unknown_reading_is_not_treated_as_ahead() -> None:
    states, _ = _states({"gmail": 0.9}, 0.5)
    states.append(ua.account_state("proton", None, NOW + WEEK / 2, NOW, BURST))
    assert states[1]["state"] == "unknown"
    verdict, detail = ua.decide_pacing(states, mode="enforce", priority=False, now=NOW, cap_s=300)
    assert verdict == "route" and detail == ["proton"]


def test_allowed_line_and_keys_stay_finite() -> None:
    allowed = ua.allowed_7d(NOW, NOW + WEEK / 2, BURST)
    assert allowed is not None and math.isfinite(allowed)
    # Decaying burst (381): the line is elapsed + burst·(1 − elapsed).
    assert allowed == pytest.approx(0.5 + BURST * 0.5)
    state = ua.account_state("a", 0.5, NOW + WEEK / 2, NOW, BURST)
    assert math.isfinite(state["ahead_by"])
    assert ua.allowed_7d(NOW, NOW - 1.0, BURST) is None  # a past reset is unknown, not zero


def test_publish_is_read_only_and_leaves_unknown_out_of_pace() -> None:
    states, _ = _states({"gmail": 0.6, "proton": 0.47}, 0.5)
    states.append(ua.account_state("x", None, NOW + WEEK / 2, NOW, BURST))
    view = ua.publish(states, mode="observe", burst=BURST, shares={"home": 0.3})
    assert view["mode"] == "observe" and view["client_shares"]["enforced"] is False
    assert view["fleet_ahead"] is True
    # used = 0.6 + 0.47 = 1.07; elapsed (allowed minus burst) = 0.5 each, so pace = 1.07 / 1.0
    assert view["fleet_pace"] == pytest.approx(1.07 / (2 * 0.5), rel=1e-3)
    assert ua.publish([], mode="off", burst=BURST, shares={})["fleet_pace"] is None


def _incident_rows(epoch_start: float, restart: float, step_at: float, reset: float):
    """Persisted meter rows for the 06/11/2026 proton shape: 0.00 for 7 h, one 1 pt step."""
    rows = []
    t = epoch_start + 60.0
    while t < restart:
        rows.append({"bearer": "p", "sampled_at": t, "util_7d": 0.0, "reset_7d": reset})
        t += 60.0
    # A stale repeat (no new header) and a torn/foreign row must both be ignored.
    rows.append(
        {"bearer": "p", "sampled_at": t - 60.0, "util_7d": 0.0, "reset_7d": reset, "stale": True}
    )
    rows.append({"bearer": "p", "sampled_at": "garbage", "util_7d": 0.5, "reset_7d": reset})
    return rows


def test_restart_without_history_reads_one_meter_step_as_overpace() -> None:
    """Reproduces the bug: history that starts at the restart turns one 1 pt step
    into a burn far above the remaining-law cap (proton read 1.78x at 0.29x)."""
    reset = 1_794_564_000.0
    epoch_start = reset - ua.WEEK_S
    restart = epoch_start + 7.4 * 3600
    step_at = restart + 45 * 60
    now = restart + 55 * 60
    live = ua.WeekSamples()
    for t in range(int(restart), int(now), 60):
        live.observe("p", float(t), 0.01 if t >= step_at else 0.0, reset)
    burn = ua.burn_per_h(live.samples("p"), now, 1800.0)
    state = ua.account_state("p", 0.01, reset, now, law="remaining", burn=burn)
    assert state["state"] == "ahead"


def test_seeded_history_keeps_burn_honest_across_a_restart() -> None:
    """With the persisted readings restored, the same step is averaged over its
    real 0.00 plateau and the account is on schedule — no fleet-wide defer."""
    reset = 1_794_564_000.0
    epoch_start = reset - ua.WEEK_S
    restart = epoch_start + 7.4 * 3600
    step_at = restart + 45 * 60
    now = restart + 55 * 60
    samples = ua.WeekSamples()
    seeded = samples.seed(_incident_rows(epoch_start, restart, step_at, reset), now)
    assert seeded > 0
    for t in range(int(restart), int(now), 60):
        samples.observe("p", float(t), 0.01 if t >= step_at else 0.0, reset)
    burn = ua.burn_per_h(samples.samples("p"), now, 1800.0)
    state = ua.account_state("p", 0.01, reset, now, law="remaining", burn=burn)
    assert state["state"] == "on_schedule", state


def test_seed_skips_stale_future_and_expired_rows() -> None:
    now = 2_000_000.0
    rows = [
        {
            "bearer": "a",
            "sampled_at": now - ua.SAMPLE_KEEP_S - 10,
            "util_7d": 0.1,
            "reset_7d": now + 9,
        },
        {
            "bearer": "a",
            "sampled_at": now - 300,
            "util_7d": 0.2,
            "reset_7d": now + 9,
            "stale": True,
        },
        {"bearer": "a", "sampled_at": now + 300, "util_7d": 0.3, "reset_7d": now + 9},
        {"bearer": "a", "sampled_at": now - 120, "util_7d": 0.4, "reset_7d": now + 9},
        {"bearer": None, "sampled_at": now - 60, "util_7d": 0.5, "reset_7d": now + 9},
    ]
    samples = ua.WeekSamples()
    assert samples.seed(rows, now) == 1
    assert samples.samples("a") == [(now - 120, 0.4, now + 9)]
