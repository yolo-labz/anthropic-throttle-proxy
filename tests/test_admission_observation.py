"""/__throttle/admission weekly_pacing: per-account observed_at + stale (additive)."""

from __future__ import annotations

import time

import pytest

from anthropic_throttle_proxy import accounts, config, proxy
from anthropic_throttle_proxy import usage_allocator as ua

NOW = 1_794_400_000.0  # 11/11/2026 09:26:40 BRT
RESET = NOW + 2 * 86_400
PRE_EXISTING = {
    "ahead_by",
    "allowed_7d",
    "burn_per_h",
    "cap_per_h",
    "elapsed",
    "label",
    "law",
    "reset_7d",
    "state",
    "util_7d",
    "wait_s",
}


@pytest.fixture
def view(monkeypatch: pytest.MonkeyPatch):
    snapshot = [
        {"label": "gmail", "bearer_id": "gggg1111"},
        {"label": "proton", "bearer_id": "pppp2222"},
    ]
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: list(snapshot))
    monkeypatch.setattr(config, "WEEKLY_BURST", ua.DEFAULT_BURST)
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "observe")
    monkeypatch.setattr(config, "WEEKLY_PACING_LAW", "remaining")  # live law: full field set
    monkeypatch.setattr(config, "bearer_state", {})

    def build(gmail_at: object, proton_at: object = NOW - 5) -> dict[str, dict]:
        for bid, util, at in (("gggg1111", 1.0, gmail_at), ("pppp2222", 0.01, proton_at)):
            state: dict = {"unified": {"util_7d": util, "reset_7d": RESET}}
            if at is not None:
                state["unified_at"] = at
            config.bearer_state[bid] = state
        return {row["label"]: row for row in proxy._weekly_pacing_view()["accounts"]}

    return build


def test_fresh_reading_publishes_its_observation_time(view) -> None:
    rows = view(NOW - 60)
    assert rows["gmail"]["observed_at"] == "2026-11-11T12:25:40Z"
    assert rows["gmail"]["stale"] is False
    assert rows["proton"]["stale"] is False


def test_reading_older_than_the_shared_bound_is_stale(view) -> None:
    rows = view(NOW - 14 * 3600)  # the 10/11 rejected reading carried 14 h
    assert rows["gmail"]["stale"] is True
    assert rows["gmail"]["observed_at"] == "2026-11-10T22:26:40Z"
    assert rows["gmail"]["util_7d"] == 1.0  # still published, now labelled
    edge = view(NOW - accounts.ENDPOINT_STALE_MAX_S)["gmail"]
    assert edge["stale"] is False
    assert view(NOW - accounts.ENDPOINT_STALE_MAX_S - 1)["gmail"]["stale"] is True


@pytest.mark.parametrize("bad", [NOW + 30, float("nan"), float("inf"), 0, -5, True, "1794400000"])
def test_future_or_invalid_clock_is_never_fresh(view, bad) -> None:
    row = view(bad)["gmail"]
    assert row["stale"] is True
    assert row["observed_at"] is None


def test_missing_observation_is_stale(view) -> None:
    row = view(None)["gmail"]
    assert row["stale"] is True
    assert row["observed_at"] is None


def test_pre_existing_fields_keep_their_values(view, monkeypatch) -> None:
    stamped = view(NOW - 60)
    monkeypatch.setattr(proxy, "_stamp_observation", lambda *_a: None)
    plain = view(NOW - 60)
    for label, row in plain.items():
        assert set(row) >= PRE_EXISTING
        assert set(stamped[label]) - set(row) == {"observed_at", "stale"}
        assert {k: stamped[label][k] for k in row} == row


def test_stale_label_never_changes_the_verdict(view, monkeypatch) -> None:
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "enforce")
    view(NOW - 60)
    fresh = proxy._weekly_pacing_view()
    view(NOW - 14 * 3600)
    aged = proxy._weekly_pacing_view()
    assert aged["retry_after_s"] == fresh["retry_after_s"]
    assert aged["fleet_ahead"] == fresh["fleet_ahead"]
