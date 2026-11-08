"""Funding observations cannot be mistaken for live authorized capacity."""

from copy import deepcopy

import pytest

from anthropic_throttle_proxy.api_credits import capacity_for, normalize

NOW = 1794157200.0


def record():
    return {
        "id": "openai:personal-api",
        "provider": "openai",
        "accountId": "personal",
        "workspaceId": "fleet",
        "currency": "USD",
        "source": "billing-console",
        "observedAt": "2026-11-08T16:59:00Z",
        "validUntil": "2026-11-08T17:10:00Z",
        "credit": {"status": "available", "remaining": 12.5},
        "authorized": True,
        "rateLimits": [
            {"model": "gpt-example", "scope": "fleet", "rpmRemaining": 5, "tpmRemaining": 1000}
        ],
        "spendLimit": {"remaining": 10, "enforced": True},
    }


def account(item=None, **kwargs):
    return normalize({"schema": 1, "accounts": [item or record()]}, NOW, **kwargs)["accounts"][0]


def test_funding_rate_and_route_are_independent():
    row = account()
    assert capacity_for(row, "gpt-example", "fleet")
    assert not capacity_for(row, "different-model", "fleet")
    assert not capacity_for(row, "gpt-example", "different-workspace")
    row["rateLimits"][0]["tpmRemaining"] = 0
    assert row["credit"]["remaining"] == 12.5
    assert not capacity_for(row, "gpt-example", "fleet")


@pytest.mark.parametrize("amount", [True, False, -1, float("inf"), float("nan"), 10**400, "12"])
def test_invalid_money_is_unknown(amount):
    item = record()
    item["credit"]["remaining"] = amount
    row = account(item)
    assert row["credit"]["status"] == "unknown"
    assert not capacity_for(row, "gpt-example", "fleet")


@pytest.mark.parametrize("status", ["pending", "unknown", "refused"])
def test_pending_credit_does_not_become_spendable(status):
    item = record()
    item["credit"]["status"] = status
    assert account(item)["credit"]["remaining"] is None
    assert not capacity_for(account(item), "gpt-example", "fleet")


def test_expiry_and_report_staleness_override_positive_wallet():
    item = record()
    item["credit"]["expiresAt"] = "2026-11-08T16:00:00Z"
    assert account(item)["credit"]["status"] == "expired"
    assert not capacity_for(account(item), "gpt-example", "fleet")
    assert account(report_stale=True)["credit"]["status"] == "stale"
    for timestamp in ("2026-11-08T18:00:00Z", "2026-11-08T16:59:00", None):
        item = record()
        item["observedAt"] = timestamp
        assert not capacity_for(account(item), "gpt-example", "fleet")


def test_authorization_and_spend_ceiling_require_explicit_facts():
    for value in (None, False, "true"):
        item = record()
        item["authorized"] = value
        assert not capacity_for(account(item), "gpt-example", "fleet")
    item = record()
    item["spendLimit"] = {"remaining": 0, "enforced": False}
    assert capacity_for(account(item), "gpt-example", "fleet")
    item["spendLimit"]["enforced"] = True
    assert not capacity_for(account(item), "gpt-example", "fleet")
    item["spendLimit"] = {}
    assert not capacity_for(account(item), "gpt-example", "fleet")


def test_version_and_provider_allowlist():
    assert normalize({"schema": True, "accounts": [record()]}, NOW)["accounts"] == []
    item = deepcopy(record())
    item["provider"] = "unfunded-other"
    assert normalize({"schema": 1, "accounts": [item]}, NOW)["accounts"] == []


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_provider_parity_and_unknown_source(provider):
    item = record()
    item["provider"] = provider
    assert capacity_for(account(item), "gpt-example", "fleet")
    item["source"] = "unverified"
    assert not capacity_for(account(item), "gpt-example", "fleet")
    assert account(item)["source"] == ""


@pytest.mark.parametrize("generated", ["2026-11-08T16:59:00Z", None, "2026-11-08T18:00:00Z"])
def test_report_reader_adds_api_accounts_without_changing_subscription(tmp_path, generated):
    import json

    from anthropic_throttle_proxy import lanes

    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "generatedAt": generated,
                "intervalSeconds": 900,
                "lanes": [],
                "apiCredits": {"schema": 1, "accounts": [record()]},
            }
        )
    )
    result = lanes._read(NOW, str(path))
    assert capacity_for(result["api_credits"]["accounts"][0], "gpt-example", "fleet") is (
        generated == "2026-11-08T16:59:00Z"
    )
    assert result["lanes"] == []
