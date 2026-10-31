"""Spec 377 remaining law (381): spend the REMAINING 7d budget evenly, not the line."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from anthropic_throttle_proxy import usage_allocator as ua

NOW = 1_000_000.0
WEEK = ua.WEEK_S
BURST = ua.DEFAULT_BURST

# Measured 15:16 on 31/10: gmail 0.29 with 7d elapsed 0.127; proton 0.32, elapsed 0.192.
# Both burn 8 pt/h. The remaining cap is (1-u)/T_left, which is far below that burn.
TODAY = {
    "gmail": {"util": 0.29, "elapsed": 0.127},
    "proton": {"util": 0.32, "elapsed": 0.192},
}


def _reset_for(elapsed: float) -> float:
    return NOW + (1.0 - elapsed) * WEEK


def _series(rate_frac_per_h: float, hours_back: float, reset: float, u0: float = 0.10):
    """1-pt meter steps at ``rate_frac_per_h``, one reading a minute (as WeekSamples keeps)."""
    out = []
    t = NOW - hours_back * 3600
    gap = 0.01 / rate_frac_per_h * 3600 if rate_frac_per_h > 0 else None
    nxt = t + gap if gap else None
    u = u0
    while t <= NOW:
        if nxt is not None and t >= nxt:
            u = round(u + 0.01, 2)
            nxt += gap
        out.append((t, u, reset))
        t += 60
    return out


# --- decaying line -----------------------------------------------------------


def test_line_allowance_decays_to_1_at_reset() -> None:
    reset = NOW + WEEK * 0.01  # 1 % of the week left
    assert ua.allowed_7d(NOW, reset, BURST) == pytest.approx(0.99 + BURST * 0.01)
    assert ua.allowed_7d(reset - 1.0, reset, BURST) == pytest.approx(1.0, abs=1e-3)


def test_line_catchup_inverts_decaying_line() -> None:
    util = 0.6
    reset = _reset_for(0.5)
    wait = ua.catchup_wait_s(util, reset, NOW, BURST)
    e_star = (util - BURST) / (1.0 - BURST)
    assert wait == pytest.approx((e_star - 0.5) * WEEK, abs=2.0)


# --- today's numbers, remaining law ------------------------------------------


@pytest.mark.parametrize("name", ["gmail", "proton"])
def test_today_at_8pt_per_h_both_ahead_under_remaining(name: str) -> None:
    cfg = TODAY[name]
    reset = _reset_for(cfg["elapsed"])
    state = ua.account_state(
        name,
        cfg["util"],
        reset,
        NOW,
        BURST,
        law="remaining",
        burn=0.08,
        rate_window_s=1800.0,
        margin=0.1,
    )
    assert state["state"] == "ahead", state
    assert state["cap_per_h"] == pytest.approx((1 - cfg["util"]) / ((reset - NOW) / 3600), abs=1e-5)
    assert state["cap_per_h"] < 0.01  # well under 1 pt/h of what is left


@pytest.mark.parametrize("name", ["gmail", "proton"])
def test_today_at_0_4pt_per_h_not_ahead_under_remaining(name: str) -> None:
    cfg = TODAY[name]
    reset = _reset_for(cfg["elapsed"])
    state = ua.account_state(
        name,
        cfg["util"],
        reset,
        NOW,
        BURST,
        law="remaining",
        burn=0.004,
        rate_window_s=1800.0,
        margin=0.1,
    )
    # Cap at 0.4 pt/h stays under the pace of the day; the burn is inside the budget.
    assert state["state"] != "ahead", state


def test_remaining_unknown_burn_is_not_ahead() -> None:
    reset = _reset_for(0.2)
    state = ua.account_state("x", 0.3, reset, NOW, BURST, law="remaining", burn=None)
    assert state["state"] == "unknown"


def test_remaining_retry_after_is_the_rate_window() -> None:
    reset = _reset_for(0.2)
    state = ua.account_state(
        "x", 0.3, reset, NOW, BURST, law="remaining", burn=0.08, rate_window_s=900.0
    )
    assert ua.catchup_retry([state], now=NOW, cap_s=1e9) == 900


# --- line law unchanged in picks (except the decay) --------------------------


def test_line_law_picks_match_plain_call() -> None:
    reset = _reset_for(0.5)
    a = ua.account_state("g", 0.70, reset, NOW, BURST)
    b = ua.account_state("g", 0.70, reset, NOW, BURST, law="line", burn=0.2)
    assert a == b


# --- weekly end-of-window replay: remaining law ends <= 1.00 ------------------


def _replay():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pace_replay.py"
    spec = importlib.util.spec_from_file_location("pace_replay", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _week(pr, law: str, demand: float, mutant_no_remaining: bool = False):
    accs = [
        {"label": lab, "util": 0.0, "reset": NOW + WEEK, "rate": 1.2 / WEEK} for lab in ("a", "b")
    ]
    win = pr.Window(0.0, NOW, WEEK + 120, law=law)
    return pr.simulate("enforce", BURST, 1, demand, accs, win)


def test_remaining_constant_demand_ends_at_or_below_1() -> None:
    pr = _replay()
    r = _week(pr, "remaining", demand=1.0)
    assert r["max_end_pace"] <= 1.0 + 1e-3, r["end_pace"]


def test_line_constant_demand_still_over_1_end_of_week() -> None:
    # Documents why remaining exists: the line closes near 1 + burst.
    pr = _replay()
    r = _week(pr, "line", demand=1.0)
    assert r["max_end_pace"] > 1.0


def test_mutant_without_remaining_factor_fails_end_of_week() -> None:
    """Mutant: drop the (1−u) factor, cap = 1/T_left. The rate then ignores spend."""
    pr = _replay()
    original = ua.remaining_cap_per_h

    def mutant(util, reset, now):
        if util is None or reset is None or reset <= now:
            return None
        return 1.0 / ((reset - now) / 3600.0)

    ua.remaining_cap_per_h = mutant
    try:
        r = _week(pr, "remaining", demand=1.0)
    finally:
        ua.remaining_cap_per_h = original
    assert r["max_end_pace"] > 1.0, r["end_pace"]


# --- burn measurement (1-pt quantisation) -------------------------------------


def test_burn_at_8pt_per_h_measured_over_window() -> None:
    reset = NOW + 6 * 86400
    samples = _series(0.08, 2.0, reset)
    assert ua.burn_per_h(samples, NOW, 1800.0) == pytest.approx(0.08, rel=0.1)


def test_burn_at_0_4pt_per_h_is_not_inflated_by_one_step() -> None:
    reset = NOW + 6 * 86400
    samples = _series(0.004, 24.0, reset)
    burn = ua.burn_per_h(samples, NOW, 1800.0)
    assert burn is not None and burn < 0.01  # not read as 2 pt/h from a single step


def test_burn_falling_meter_is_zero_not_negative() -> None:
    reset = NOW + 6 * 86400
    samples = [(NOW - 3600, 0.30, reset), (NOW, 0.29, reset)]
    assert ua.burn_per_h(samples, NOW, 1800.0) == 0.0


def test_parse_law_and_knobs_are_finite() -> None:
    assert ua.parse_law("remaining") == "remaining"
    assert ua.parse_law("bogus") == "line"
    assert ua.parse_rate_window("nan") == ua.DEFAULT_RATE_WINDOW_S
    assert ua.parse_rate_margin("-1") == ua.DEFAULT_RATE_MARGIN


def test_publish_fleet_pace_uses_elapsed_under_both_laws() -> None:
    """Regression: publish must not invert the display allowance under the remaining law."""
    reset = _reset_for(0.5)
    line = [ua.account_state("a", 0.5, reset, NOW, BURST)]
    rem = [ua.account_state("a", 0.5, reset, NOW, BURST, law="remaining", burn=0.0)]
    kw = {"mode": "enforce", "burst": BURST, "shares": {}}
    p_line = ua.publish(line, **kw)["fleet_pace"]
    p_rem = ua.publish(rem, **kw)["fleet_pace"]
    assert p_line == pytest.approx(0.5 / 0.5, abs=1e-3)
    assert p_rem == pytest.approx(0.5 / 0.5, abs=1e-3)
