"""Spec 377 phase 2: attribution, share enforcement (fleet-ahead gated), multi-window burn."""

from __future__ import annotations

import time
from typing import cast

import pytest
from multidict import CIMultiDict

from anthropic_throttle_proxy import accounts, config, proxy
from anthropic_throttle_proxy import usage_allocator as ua
from anthropic_throttle_proxy.project_share import PROJECT_HEADER

WEEK = ua.WEEK_S


# --- attribution -------------------------------------------------------------


def test_absent_header_and_label_is_direct() -> None:
    assert ua.weekly_project(None, None) == "_direct"


def test_project_header_wins_over_workspace_label() -> None:
    assert ua.weekly_project("Side Projects", "ws-other") == "side-projects"


def test_workspace_label_is_the_fallback() -> None:
    assert ua.weekly_project(None, "Pedro-Workspace") == "pedro-workspace"


@pytest.mark.parametrize("header", ["   ", "***", ""])
def test_unusable_header_falls_to_direct(header: str) -> None:
    assert ua.weekly_project(header, None) == "_direct"


def test_look_alike_of_the_exempt_bucket_is_not_exempt() -> None:
    # The leading underscore is stripped, so "_direct" cannot claim the exemption.
    assert ua.weekly_project("_direct", None) == "direct"


def test_project_header_is_stripped_before_upstream() -> None:
    class Req:
        headers = CIMultiDict(
            {
                PROJECT_HEADER: "side-projects",
                "X-Anthropic-Throttle-Project": "dup",
                "anthropic-version": "2023-06-01",
            }
        )

    fwd = proxy._forward_headers(Req())
    assert not [k for k in fwd if k.lower() == PROJECT_HEADER]
    assert fwd.get("anthropic-version") == "2023-06-01"


# --- measured tokens ---------------------------------------------------------


def test_zero_and_absent_usage_is_never_recorded() -> None:
    ledger = ua.WeeklyTokens()
    ledger.add("side-projects", 0, 1e6)
    ledger.add("side-projects", -5, 1e6)
    ledger.add("side-projects", cast(int, None), 1e6)
    assert ledger.totals(1e6) == {}
    ledger.add("side-projects", 500, 1e6)
    assert ledger.totals(1e6) == {"side-projects": 500}


def test_tokens_age_out_of_the_seven_day_window() -> None:
    t = ua.WeeklyTokens()
    now = 10 * WEEK
    t.add("old", 1000, now - WEEK - 7200)
    t.add("new", 300, now - 60)
    assert t.totals(now) == {"new": 300}


# --- share enforcement -------------------------------------------------------

BURST = ua.DEFAULT_BURST


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> float:
    view = [
        {"label": name, "bearer_id": bid}
        for name, bid in (("gmail", "gggg1111"), ("proton", "pppp2222"))
    ]
    settings = {
        "WEEKLY_BURST": BURST,
        "QUEUE_RETRY_AFTER_MAX_S": 10**9,
        "WEEKLY_PACING_MODE": "enforce",
        "CLIENT_SHARES": {"side-projects": 0.4, "other": 0.3},
    }
    for key, value in settings.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: list(view))
    monkeypatch.setattr(proxy, "_WEEKLY_TOKENS", ua.WeeklyTokens())
    config.bearer_state.clear()
    return time.time()


def _set_util(now: float, gmail: float, proton: float, elapsed: float = 0.5) -> None:
    reset = now + (1.0 - elapsed) * WEEK
    config.bearer_state["gggg1111"] = {"unified": {"util_7d": gmail, "reset_7d": reset}}
    config.bearer_state["pppp2222"] = {"unified": {"util_7d": proton, "reset_7d": reset}}


def _skewed_tokens() -> None:
    proxy._WEEKLY_TOKENS.add("side-projects", 900, time.time())
    proxy._WEEKLY_TOKENS.add("other", 100, time.time())


def test_over_share_client_is_deferred_when_fleet_is_ahead(fleet) -> None:
    _set_util(fleet, 0.60, 0.47)  # Σutil 1.07 over Σelapsed 1.0 → fleet ahead
    _skewed_tokens()
    retry = proxy._weekly_share_defer("side-projects", priority=False)
    assert isinstance(retry, int) and 1 <= retry <= 10**9


def test_over_share_client_is_served_when_fleet_is_on_schedule(fleet) -> None:
    _set_util(fleet, 0.40, 0.40)  # Σutil 0.80 → fleet under schedule: borrow when idle
    _skewed_tokens()
    assert proxy._weekly_share_defer("side-projects", priority=False) is None


def test_priority_calls_are_exempt(fleet) -> None:
    _set_util(fleet, 0.60, 0.47)
    _skewed_tokens()
    assert proxy._weekly_share_defer("side-projects", priority=True) is None


def test_unlisted_and_direct_clients_are_never_share_deferred(fleet) -> None:
    _set_util(fleet, 0.60, 0.47)
    _skewed_tokens()
    assert proxy._weekly_share_defer("_direct", priority=False) is None
    assert proxy._weekly_share_defer("other", priority=False) is None


def test_observe_mode_never_defers(fleet, monkeypatch) -> None:
    _set_util(fleet, 0.60, 0.47)
    _skewed_tokens()
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "observe")
    assert proxy._weekly_share_defer("side-projects", priority=False) is None


def test_idle_siblings_share_is_borrowed_not_hoarded(fleet) -> None:
    _set_util(fleet, 0.60, 0.47)  # fleet ahead
    proxy._WEEKLY_TOKENS.add("side-projects", 900, time.time())  # only active client
    assert proxy._weekly_share_defer("side-projects", priority=False) is None


def test_no_measured_usage_means_no_defer(fleet) -> None:
    _set_util(fleet, 0.60, 0.47)  # fleet ahead, but nothing measured yet
    assert proxy._weekly_share_defer("side-projects", priority=False) is None


# --- multi-window burn -------------------------------------------------------


def test_sustained_1_20x_pace_fires_both_windows() -> None:
    now = 1_000_000.0 + WEEK * 0.5
    reset = now + WEEK * 0.5  # half the week gone, half left
    start = reset - WEEK

    def util(t: float) -> float:
        return 1.20 * (t - start) / WEEK  # exactly 1.20× elapsed pace

    store = ua.WeekSamples()
    t = now - 25 * 3600.0
    while t <= now:
        store.observe("gggg1111", t, util(t), reset)
        t += 60.0
    alert = ua.burn_alert(store.samples("gggg1111"), now)
    assert alert["firing"] is True
    assert alert["long_24h"] == pytest.approx(1.2, abs=0.02)
    assert alert["short_6h"] == pytest.approx(1.2, abs=0.02)


def test_single_5h_spike_does_not_fire_but_would_fool_a_single_window() -> None:
    now = 1_000_000.0
    reset = now + 4 * 24 * 3600.0  # four days left, plenty of runway

    def util(t: float) -> float:
        ramp_start = now - 1800.0  # the spike lands in the last 30 minutes
        if t <= ramp_start:
            return 0.30
        return 0.30 + 0.06 * (t - ramp_start) / 1800.0

    store = ua.WeekSamples()
    t = now - 25 * 3600.0
    while t <= now:
        store.observe("gggg1111", t, util(t), reset)
        t += 60.0
    samples = store.samples("gggg1111")
    alert = ua.burn_alert(samples, now)
    assert alert["firing"] is False
    # Mutant check: the short window alone would have fired on this spike.
    short_only = ua.burn_projection(samples, now, ua.BURN_SHORT_S)
    assert short_only is not None and short_only > 1.0


def test_burn_is_silent_without_enough_span_or_across_a_reset() -> None:
    now = 1_000_000.0
    assert ua.burn_projection([], now, ua.BURN_LONG_S) is None
    store = ua.WeekSamples()
    store.observe("k", now - 30, 0.5, now + WEEK)
    assert ua.burn_projection(store.samples("k"), now, ua.BURN_SHORT_S) is None


# --- wiring: one gate for the handler, burn metric/log transition -------------


def test_defer_response_is_one_gate_and_the_share_reason(fleet) -> None:
    _set_util(fleet, 0.60, 0.47)
    _skewed_tokens()
    resp = proxy._weekly_defer_response(False, "side-projects")
    assert resp is not None and resp.status == 503
    assert b"weekly share" in resp.body
    assert proxy._weekly_defer_response(True, "side-projects") is None


def test_pacing_view_publishes_tokens_burn_and_enforced_flag(fleet, monkeypatch) -> None:
    _set_util(fleet, 0.60, 0.47)
    _skewed_tokens()
    view = proxy._weekly_pacing_view()
    assert view["weekly_tokens"] == {"side-projects": 900, "other": 100}
    assert view["client_shares"]["enforced"] is True
    assert view["client_shares"]["phase"] == "enforced-when-fleet-ahead"
    assert view["burn"] == {}  # no unified samples observed in this test
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "observe")
    assert proxy._weekly_pacing_view()["client_shares"]["phase"] == "observe-only"


def test_burn_transition_logs_and_counts_once(monkeypatch) -> None:
    now = 1_000_000.0 + WEEK * 0.5
    reset = now + WEEK * 0.5
    start = reset - WEEK
    store = ua.WeekSamples()
    t = now - 25 * 3600.0
    while t <= now:
        store.observe("hot", t, 1.20 * (t - start) / WEEK, reset)
        t += 60.0
    monkeypatch.setattr(proxy, "_WEEK_SAMPLES", store)
    monkeypatch.setattr(proxy, "_burn_firing", set())
    logged: list[str] = []
    monkeypatch.setattr(proxy, "log", logged.append)
    before = proxy.M_WEEKLY_BURN_ALERTS.labels(bearer="hot")._value.get()
    proxy._refresh_burn(now, force=True)
    proxy._refresh_burn(now + 1, force=True)  # still firing: no second transition
    after = proxy.M_WEEKLY_BURN_ALERTS.labels(bearer="hot")._value.get()
    assert after - before == 1
    assert len(logged) == 1 and "weekly-burn-alert bid=hot" in logged[0]
    assert proxy.M_WEEKLY_BURN.labels(bearer="hot", window="24h")._value.get() > 1.0
