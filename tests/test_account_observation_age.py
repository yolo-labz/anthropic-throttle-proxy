"""Quota samples retain their own age through the account/dashboard join."""

import pytest

from anthropic_throttle_proxy import accounts
from anthropic_throttle_proxy.ui import presentation, routes

NOW = 1_794_367_000.0
BID = "12345678"


def account_row(monkeypatch, observed_at, *, used=1.0):
    monkeypatch.setattr(accounts, "_endpoint_cache", {})
    monkeypatch.setattr(
        accounts,
        "account_snapshot",
        lambda: [
            {
                "label": "A",
                "path": "/synthetic/account.json",
                "bearer_id": BID,
                "expires_at": None,
                "error": None,
            }
        ],
    )
    monkeypatch.setattr(accounts, "guard_email", lambda _path: (None, False))
    bearer = {
        "bearer_id": BID,
        "unified_at": observed_at,
        "unified": {
            "util_7d": used,
            "status_7d": "rejected" if used == 1 else "allowed",
            "reset_7d": NOW + 86400,
        },
    }
    view = accounts.account_view([bearer], NOW)
    return routes._build_subscriptions(view, {"lanes": []}, NOW)[0]


@pytest.mark.parametrize("used", [0.1, 1.0])
def test_old_message_headers_are_stale_capacity_not_current_quota(monkeypatch, used):
    row = account_row(monkeypatch, NOW - 19000, used=used)
    assert row["status"] == "stale"
    assert row["sample_age_s"] == 19000
    assert row["sample_observed_at"]
    assert not row["routing_eligible"]
    summary = presentation.capacity_summary({"subscriptions": [row]})
    assert summary["counts"]["stale"] == 1
    assert summary["counts"]["usable"] == 0
    assert summary["counts"]["exhausted"] == 0


def test_recent_weekly_rejection_stays_rejected(monkeypatch):
    row = account_row(monkeypatch, NOW - 10)
    assert row["status"] == "rejected"
    assert row["sample_age_s"] == 10


@pytest.mark.parametrize("observed_at", [None, True, float("nan"), float("inf"), NOW + 60])
def test_invalid_sample_clock_never_claims_current_capacity(monkeypatch, observed_at):
    row = account_row(monkeypatch, observed_at, used=0.1)
    assert row["status"] == "stale"
    assert row["sample_age_s"] is None
    assert not row["routing_eligible"]


async def test_collect_view_preserves_message_observation_age(monkeypatch):
    """A fresh dashboard collection must not refresh old provider evidence."""
    account_row(monkeypatch, NOW - 19000)
    monkeypatch.setattr(routes.time, "time", lambda: NOW)
    monkeypatch.setattr(accounts, "bearer_labels", lambda: {BID: "A"})
    monkeypatch.setattr(
        routes._proxy,
        "bearer_state",
        {
            BID: {
                "unified_at": NOW - 19000,
                "unified": {
                    "util_7d": 1.0,
                    "status_7d": "rejected",
                    "reset_7d": NOW + 86400,
                },
            }
        },
    )
    monkeypatch.setattr(routes._proxy, "bearer_limiters", {})
    monkeypatch.setattr(routes._proxy, "_bearer_credential", lambda _bid: None)
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda *_args: None)
    monkeypatch.setattr(routes, "_cached_fleet", lambda _now: [])
    monkeypatch.setattr(routes, "_cached_copilot", lambda _now: [])
    monkeypatch.setattr(routes._lanes, "view", lambda _now: {"lanes": []})
    monkeypatch.setattr(
        routes._fleet_ui_config,
        "load",
        lambda: {
            "subscriptions": [],
            "defaults": {"emoji_by_family": {}, "hidden_families": [], "show_primary": True},
        },
    )
    collected_accounts = []
    real_account_view = accounts.account_view

    def capture_accounts(*args):
        rows = real_account_view(*args)
        collected_accounts.extend(rows)
        return rows

    monkeypatch.setattr(accounts, "account_view", capture_accounts)
    view = await routes._collect_view()

    assert collected_accounts[0]["sample_age_s"] == 19000
    assert collected_accounts[0]["reading_stale"] is True
    row = view["subscriptions"][0]
    assert row["sample_age_s"] == 19000
    assert row["status"] == "stale"
    assert not row["routing_eligible"]
    assert view["summary"]["counts"]["stale"] == 1
    assert view["summary"]["counts"]["usable"] == 0
    assert view["summary"]["counts"]["exhausted"] == 0
