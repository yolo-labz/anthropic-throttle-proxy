"""Spec 377 phase 1: proxy wiring of the pacing rules, offline (fake snapshot + readings)."""

from __future__ import annotations

import time

import pytest

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy import usage_allocator as ua


@pytest.fixture
def two_accounts(monkeypatch: pytest.MonkeyPatch):
    now = time.time()
    snapshot = [
        {"label": "gmail", "bearer_id": "gggg1111"},
        {"label": "proton", "bearer_id": "pppp2222"},
    ]
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    from anthropic_throttle_proxy import accounts

    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: list(snapshot))
    monkeypatch.setattr(config, "QUEUE_RETRY_AFTER_MAX_S", 10**9)
    monkeypatch.setattr(config, "WEEKLY_BURST", ua.DEFAULT_BURST)
    # Swap in a private dict for this test only: writing into the shared
    # config.bearer_state leaked bearers into later tests (test_accounts ordering).
    monkeypatch.setattr(config, "bearer_state", {})
    return now, snapshot


def _set_util(now: float, gmail: float, proton: float, elapsed: float) -> None:
    reset = now + (1.0 - elapsed) * ua.WEEK_S
    config.bearer_state["gggg1111"] = {"unified": {"util_7d": gmail, "reset_7d": reset}}
    config.bearer_state["pppp2222"] = {"unified": {"util_7d": proton, "reset_7d": reset}}


def test_observe_and_off_return_the_snapshot_unchanged(monkeypatch, two_accounts) -> None:
    now, snapshot = two_accounts
    _set_util(now, 0.60, 0.47, 0.5)  # gmail ahead, proton on schedule
    for mode in ("off", "observe"):
        monkeypatch.setattr(config, "WEEKLY_PACING_MODE", mode)
        assert proxy._weekly_pacing_filter(snapshot, now) is snapshot
        assert proxy._weekly_pacing_defer(priority=False) is None


def test_enforce_drops_ahead_account_while_sibling_has_room(monkeypatch, two_accounts) -> None:
    now, snapshot = two_accounts
    _set_util(now, 0.60, 0.47, 0.5)
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "enforce")
    kept = proxy._weekly_pacing_filter(snapshot, now)
    assert [a["label"] for a in kept] == ["proton"]


def test_all_ahead_defers_non_priority_and_passes_priority(monkeypatch, two_accounts) -> None:
    now, snapshot = two_accounts
    _set_util(now, 0.70, 0.60, 0.5)
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "enforce")
    assert proxy._weekly_pacing_filter(snapshot, now) is snapshot  # no spill target: no filter
    retry = proxy._weekly_pacing_defer(priority=False)
    expected = min(
        ua.catchup_wait_s(v, now + 0.5 * ua.WEEK_S, now, ua.DEFAULT_BURST) for v in (0.70, 0.60)
    )
    assert retry is not None and abs(retry - expected) <= 1.0
    assert proxy._weekly_pacing_defer(priority=True) is None


def test_deferred_response_is_the_stamped_503_with_the_computed_retry_after() -> None:
    resp = proxy._weekly_pacing_response(4242)
    assert resp.status == 503
    assert resp.headers["retry-after"] == "4242"
    assert resp.headers[config.QUEUE_TIMEOUT_HEADER] == "1"


def test_admission_view_is_read_only_marks_shares_observe_only(monkeypatch, two_accounts) -> None:
    now, _ = two_accounts
    _set_util(now, 0.60, 0.47, 0.5)
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "observe")
    monkeypatch.setattr(config, "CLIENT_SHARES", {"side-projects": 0.4})
    view = proxy._weekly_pacing_view()
    assert view["mode"] == "observe" and view["fleet_ahead"] is True
    assert view["client_shares"] == {
        "configured": {"side-projects": 0.4},
        "enforced": False,
        "phase": "observe-only",
    }
    assert {a["label"] for a in view["accounts"]} == {"gmail", "proton"}
