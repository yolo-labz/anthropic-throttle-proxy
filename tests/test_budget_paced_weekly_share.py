"""budget_paced weekly-share routing (weekly-pace-routing brief).

Per-account selection in ``budget_paced`` is proportional to each account's
remaining 7d allowance RATE (headroom / hours-to-reset), penalised once the
account's 7d pace exceeds 1x. ``least_loaded`` must not change.
"""

import math

import pytest

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter

WEEK = 7 * 86400.0


def _acct(bid: str) -> dict:
    return {"bearer_id": bid, "endpoint": None}


def _setup(monkeypatch, mode: str, now: float, accounts: dict[str, dict]) -> None:
    """accounts: bid -> {util_7d, reset_in_s, inflight, queued}."""
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", mode)
    monkeypatch.setattr(config, "bearer_state", {}, raising=False)
    limiters = {}
    for bid, spec in accounts.items():
        config.bearer_state[bid] = {
            "unified": {
                "status": "allowed",
                "util_7d": spec["util_7d"],
                "status_7d": "allowed",
                "reset_7d": now + spec["reset_in_s"],
                "util_5h": 0.0,
                "status_5h": "allowed",
            },
            "last_ratelimit": {},
        }
        lim = FairBearerLimiter(32, "fair")
        lim.inflight = spec.get("inflight", 0)
        # queued_total is derived from _queues; parked placeholders only when asked.
        for i in range(spec.get("queued", 0)):
            lim._queues.setdefault(f"q{i}", __import__("collections").deque()).append(object())
        limiters[bid] = lim
    monkeypatch.setattr(config, "bearer_limiters", limiters, raising=False)


def _pick(accounts: dict[str, dict], now: float) -> str:
    scores = {
        bid: proxy._account_routing_candidate_score(_acct(bid), "", now=now) for bid in accounts
    }
    return min(scores, key=scores.get)


def test_weight_matches_headroom_over_hours_at_pace_one():
    now = 1_000_000.0
    # 50% used with exactly 50% of the window elapsed: pace 1.0, no penalty.
    w = proxy._weekly_share_weight(0.5, now + WEEK * 0.5, now)
    assert w == pytest.approx(0.5 / ((WEEK * 0.5) / 3600.0))


def test_pace_penalty_scales_weight_and_zeroes_at_one_point_five():
    now = 1_000_000.0
    reset = now + WEEK * 0.5  # elapsed 0.5
    unpenalised = proxy._weekly_share_weight(0.20, reset, now)  # pace 0.4
    over_12 = proxy._weekly_share_weight(0.30, reset, now)  # pace 0.6 -> no penalty
    assert over_12 > 0 and unpenalised > 0
    # pace 1.2 (util 0.6 at elapsed 0.5): scale = 1 - 2*0.2 = 0.6 of headroom-rate
    w12 = proxy._weekly_share_weight(0.60, reset, now)
    headroom_rate = (1 - 0.60) / ((WEEK * 0.5) / 3600.0)
    assert w12 == pytest.approx(headroom_rate * 0.6)
    # pace 1.5 and above -> zero share while over pace
    assert proxy._weekly_share_weight(0.75, reset, now) == pytest.approx(0.0)


def test_13_35_dashboard_numbers_prefer_under_pace_proton(monkeypatch):
    """gmail 7d 14% resets in 6d4h (pace 1.20); proton 17% resets in 5d17h (0.94).

    Deterministic preference, not a proportional split: an idle, tied pair always
    sends the next request to the higher-weight (under-pace) account.
    """
    now = 1_000_000.0
    gmail_reset = 6 * 86400 + 4 * 3600.0
    proton_reset = 5 * 86400 + 17 * 3600.0
    accounts = {
        "gm": {"util_7d": 0.14, "reset_in_s": gmail_reset},
        "pr": {"util_7d": 0.17, "reset_in_s": proton_reset},
    }
    _setup(monkeypatch, "budget_paced", now, accounts)
    w_gm = proxy._weekly_share_weight(0.14, now + gmail_reset, now)
    w_pr = proxy._weekly_share_weight(0.17, now + proton_reset, now)
    assert w_pr > w_gm  # proton has more remaining rate per hour
    assert _pick(accounts, now) == "pr"


def test_over_pace_account_gets_fewer_picks_than_under_pace(monkeypatch):
    now = 1_000_000.0
    reset = WEEK * 0.5
    accounts = {
        "a": {"util_7d": 0.60, "reset_in_s": reset},
        "b": {"util_7d": 0.20, "reset_in_s": reset},
    }
    _setup(monkeypatch, "budget_paced", now, accounts)
    picks = {"a": 0, "b": 0}
    for _ in range(100):
        bid = _pick(accounts, now)
        picks[bid] += 1
    assert picks["a"] < picks["b"]


def test_zero_share_account_used_only_when_nothing_else_usable(monkeypatch):
    now = 1_000_000.0
    reset = WEEK * 0.5
    accounts = {
        "over": {"util_7d": 0.80, "reset_in_s": reset},  # pace 1.6 -> zero share
        "ok": {"util_7d": 0.20, "reset_in_s": reset},
    }
    _setup(monkeypatch, "budget_paced", now, accounts)
    assert _pick(accounts, now) == "ok"
    # Make the healthy account unusable (5h rejected -> inf); spill to 'over'.
    config.bearer_state["ok"]["unified"]["status"] = "rejected"
    config.bearer_state["ok"]["unified"]["status_5h"] = "rejected"
    over_score = proxy._account_routing_candidate_score(_acct("over"), "", now=now)
    assert math.isfinite(over_score)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda u: u.update(status_5h="rejected"),
        lambda u: u.update(util_7d=1.0),
        lambda u: u.update(status="rejected"),
    ],
)
def test_hard_gates_still_return_inf(monkeypatch, mutate):
    now = 1_000_000.0
    accounts = {"x": {"util_7d": 0.10, "reset_in_s": WEEK * 0.5}}
    _setup(monkeypatch, "budget_paced", now, accounts)
    mutate(config.bearer_state["x"]["unified"])
    assert proxy._account_routing_candidate_score(_acct("x"), "", now=now) == math.inf


def test_least_loaded_ignores_weekly_share(monkeypatch):
    """least_loaded keeps its raw utilisation ranking, unaffected by weekly pace."""
    now = 1_000_000.0
    reset = WEEK * 0.5
    accounts = {
        "a": {"util_7d": 0.60, "reset_in_s": reset},
        "b": {"util_7d": 0.20, "reset_in_s": reset},
    }
    _setup(monkeypatch, "least_loaded", now, accounts)
    sa = proxy._account_routing_candidate_score(_acct("a"), "", now=now)
    sb = proxy._account_routing_candidate_score(_acct("b"), "", now=now)
    assert sb < sa  # lower utilisation wins, as before


def test_mutant_flat_weight_loses_the_preference(monkeypatch):
    """Mutant guard: weight := 1.0 must stop the higher-weight account winning.

    The over-pace account is inserted first: with the real weight the under-pace
    account still wins; with a flat weight the tie falls back to insertion order.
    """
    now = 1_000_000.0
    reset = WEEK * 0.5
    accounts = {
        "hi": {"util_7d": 0.70, "reset_in_s": reset},
        "lo": {"util_7d": 0.10, "reset_in_s": reset},
    }
    _setup(monkeypatch, "budget_paced", now, accounts)
    assert _pick(accounts, now) == "lo"
    monkeypatch.setattr(proxy, "_weekly_share_weight", lambda *a, **k: 1.0)
    assert _pick(accounts, now) == "hi"
