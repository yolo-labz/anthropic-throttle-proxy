"""Owned API overflow requires fresh funding bound to the configured account."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anthropic_throttle_proxy import config, proxy

MODEL = "claude-sonnet-4-6"
NOW = 1794171600


@pytest.fixture
def funded(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "UPSTREAM", "https://api.anthropic.com")
    key = tmp_path / "key"
    key.write_text("synthetic-key")
    cache = tmp_path / "credits.json"
    for name, value in {
        "API_KEY_FILE": str(key),
        "API_KEY_ROUTING_MODE": "overflow",
        "API_KEY_ACCOUNT_ID": "owned",
        "API_KEY_WORKSPACE_ID": "workspace",
        "API_KEY_SCOPE": "fleet",
        "API_CREDITS_FILE": str(cache),
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(proxy.time, "time", lambda: NOW)
    monkeypatch.setattr(proxy, "_bearer_retry_after_remaining", lambda _bid: 0)
    monkeypatch.setattr(proxy, "_api_key_cache", None)
    account = {
        "id": "receipt",
        "provider": "anthropic",
        "accountId": "owned",
        "workspaceId": "workspace",
        "currency": "USD",
        "source": "billing-console",
        "observedAt": datetime.fromtimestamp(NOW - 1, UTC).isoformat(),
        "validUntil": datetime.fromtimestamp(NOW + 60, UTC).isoformat(),
        "credit": {"status": "available", "remaining": 2},
        "spendLimit": {"remaining": 2, "enforced": True},
        "authorized": True,
        "rateLimits": [{"model": MODEL, "scope": "fleet", "rpmRemaining": 5, "tpmRemaining": 5000}],
    }

    def write(item=account):
        cache.write_text(json.dumps({"schema": 1, "accounts": [item]}))

    write()
    return account, write, cache


def test_fresh_matching_candidate(funded):
    assert proxy._api_key_candidate(MODEL)["token"] == Path(config.API_KEY_FILE).read_text()
    assert proxy._api_key_candidate() is None
    assert proxy._api_key_candidate("different-model") is None


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "openai"},
        {"accountId": "other"},
        {"workspaceId": "other"},
        {"authorized": False},
        {"validUntil": "2000-01-01T00:00:00Z"},
        {"credit": {"status": "pending", "remaining": 10}},
        {"credit": {"status": "available", "remaining": 0}},
        {"spendLimit": {"enforced": True, "remaining": 0}},
        {"rateLimits": [{"model": MODEL, "scope": "other", "rpmRemaining": 1, "tpmRemaining": 10}]},
        {"rateLimits": [{"model": MODEL, "scope": "fleet", "rpmRemaining": 0, "tpmRemaining": 10}]},
    ],
)
def test_unknown_unfunded_or_unrelated_receipt_denies(funded, change):
    account, write, _ = funded
    write(copy.deepcopy(account) | change)
    assert proxy._api_key_candidate(MODEL) is None


def test_cache_changes_rechecked_without_restart(funded):
    account, write, cache = funded
    assert proxy._api_key_candidate(MODEL)
    write(account | {"authorized": False})
    assert proxy._api_key_candidate(MODEL) is None
    cache.write_text("{")
    assert proxy._api_key_candidate(MODEL) is None


def test_explicit_caller_key_preserves_headers(funded, monkeypatch):
    def forbidden(*args):
        pytest.fail("owned funding must not gate explicit caller credentials")

    monkeypatch.setattr(proxy, "_api_key_candidate", forbidden)
    headers = {"x-api-key": "caller-key", "x-test": "unchanged"}
    before = dict(headers)
    assert proxy._route_account_if_enabled(
        headers, "caller", method="POST", path="v1/messages", model=MODEL
    ) == ("caller", None)
    assert headers == before


def test_duplicate_receipts_and_missing_cache_fail_closed(funded):
    account, _, cache = funded
    cache.write_text(json.dumps({"schema": 1, "accounts": [account, account]}))
    assert proxy._api_key_candidate(MODEL) is None
    cache.unlink()
    assert proxy._api_key_candidate(MODEL) is None


def test_missing_binding_and_retry_after_fail_closed(funded, monkeypatch):
    monkeypatch.setattr(proxy, "_bearer_retry_after_remaining", lambda _bid: 60)
    assert proxy._api_key_candidate(MODEL) is None
    monkeypatch.setattr(proxy, "_bearer_retry_after_remaining", lambda _bid: 0)
    monkeypatch.setattr(config, "API_KEY_ACCOUNT_ID", "")
    assert proxy._api_key_candidate(MODEL) is None


@pytest.mark.parametrize(
    "upstream",
    [
        "https://api.moonshot.ai",
        "https://api.deepseek.com",
        "https://api.anthropic.com.example.org",
        "https://other.example/api.anthropic.com",
    ],
)
def test_other_provider_owned_key_contract_unchanged(funded, monkeypatch, upstream):
    _, _, cache = funded
    cache.unlink()
    monkeypatch.setattr(config, "UPSTREAM", upstream)
    assert proxy._api_key_candidate()["token"] == Path(config.API_KEY_FILE).read_text()


def test_oversized_metadata_fails_closed(funded):
    account, _, cache = funded
    cache.write_text(
        json.dumps({"schema": 1, "accounts": [account], "padding": " " * (256 * 1024)})
    )
    assert proxy._api_key_candidate(MODEL) is None
