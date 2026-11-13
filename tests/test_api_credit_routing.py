"""Owned API overflow requires fresh funding bound to the configured account."""

import copy
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anthropic_throttle_proxy import config, metered_spend, proxy
from anthropic_throttle_proxy.ratelimit import API_KEY_BEARER_ID

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
        "API_KEY_USD_CEILING": 10.0,
        "API_KEY_SPEND_FILE": str(tmp_path / "spend.json"),
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
    # Model-less routing is opt-in: reads such as GET /v1/models carry no model
    # and only the metered key can serve them.
    assert proxy._api_key_candidate() is None
    assert proxy._api_key_candidate(allow_model_less=True)
    # A priced model the collector never sampled is a sampling gap, not a spent
    # window. An UNPRICED model is refused: its USD spend could never be counted
    # against the ceiling (test_metered_usd_ceiling.py).
    assert proxy._api_key_candidate("claude-haiku-4-5")
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


# --- USD ceiling on the owned metered key (09/11: $299.27 in 69 min, $200 credit) ---

WINDOW = 86400.0


@pytest.fixture
def logs(monkeypatch):
    lines = []
    monkeypatch.setattr(proxy, "log", lines.append)
    monkeypatch.setattr(proxy, "_metered_refusal_logged", "")
    return lines


def spend(usd, at=NOW):
    assert metered_spend.record(config.API_KEY_SPEND_FILE, usd, WINDOW, at)


def test_spend_below_ceiling_allows_metered_candidate(funded, logs):
    spend(9.99)
    candidate = proxy._api_key_candidate(MODEL)
    assert candidate is not None and candidate["auth_type"] == "api_key"


def test_at_ceiling_refuses_with_logged_reason_and_oauth_unaffected(funded, logs, monkeypatch):
    spend(10.0)
    assert proxy._api_key_candidate(MODEL) is None
    assert logs == [
        "api-key-candidate refused reason=usd-ceiling spend=10.00 ceiling=10.00 window_s=86400"
    ]
    # Overflow: the router picks OAuth first and only falls back to `api_key`.
    oauth = {"bearer_id": "acct-a", "token": "oauth-token"}
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_pinned_account", lambda *a, **k: None)
    monkeypatch.setattr(proxy, "_account_selection", lambda *a, **k: (oauth, 0.0))
    monkeypatch.setattr(proxy, "_healthy_known_unconfigured_bearer", lambda *a, **k: False)
    route = dict(
        explicit_api_key=False, model=MODEL, max_tokens=1, now=NOW, allow_retry_probe=False
    )
    assert proxy._account_route_decision(
        [oauth], "acct-a", api_key=proxy._api_key_candidate(MODEL), **route
    ) == (oauth, False)
    # No OAuth account dispatchable: the refused metered key is NOT the escape.
    monkeypatch.setattr(proxy, "_account_selection", lambda *a, **k: (None, None))
    chosen, _ = proxy._account_route_decision(
        [oauth], "acct-a", api_key=proxy._api_key_candidate(MODEL), **route
    )
    assert chosen is None or chosen.get("auth_type") != "api_key"


def test_overflow_without_ceiling_does_not_route(funded, logs, monkeypatch):
    monkeypatch.setattr(config, "API_KEY_USD_CEILING", 0.0)
    assert config.API_KEY_ROUTING_MODE == "overflow"
    assert proxy._api_key_candidate(MODEL) is None
    assert logs == ["api-key-candidate refused reason=no-usd-ceiling"]


def test_window_spend_survives_restart(funded, logs, monkeypatch):
    spend(6.0)
    spend(4.0, at=NOW + 61)
    importlib.reload(metered_spend)  # in-process state gone, as after a restart
    monkeypatch.setattr(proxy, "_metered_spend", metered_spend)
    assert metered_spend.window_spend(config.API_KEY_SPEND_FILE, WINDOW, NOW + 61) == 10.0
    assert proxy._api_key_candidate(MODEL) is None


def test_recorded_from_real_usage_path_only_for_owned_anthropic_key(funded, logs):
    body = b'data: {"type":"message_delta","usage":{"input_tokens":1000000,"output_tokens":0}}\n'
    proxy._record_usage(MODEL, MODEL, bytearray(body), "v1/messages", seat="acct-a")
    assert metered_spend.window_spend(config.API_KEY_SPEND_FILE, WINDOW, NOW) == 0
    proxy._record_usage(MODEL, MODEL, bytearray(body), "v1/messages", seat=API_KEY_BEARER_ID)
    assert metered_spend.window_spend(config.API_KEY_SPEND_FILE, WINDOW, NOW) > 0


@pytest.mark.parametrize(
    ("incoming", "recorded"),
    [
        ({"x-api-key": "callers-own-third-party-key"}, False),
        ({"Authorization": "Bearer oauth-token"}, True),  # owned key replaced OAuth
        ({"x-api-key": "local-sentinel"}, True),  # sentinel asks for routing
    ],
)
def test_caller_key_shares_bearer_id_but_never_counts_as_owned_spend(
    funded, logs, monkeypatch, incoming, recorded
):
    from multidict import CIMultiDict

    from anthropic_throttle_proxy.ratelimit import _bearer_id

    monkeypatch.setattr(config, "LOCAL_API_KEY_SENTINEL", "local-sentinel")
    headers = CIMultiDict(incoming)
    if "x-api-key" in headers:
        # The hazard: any caller x-api-key maps to the owned key's bearer id.
        assert _bearer_id(headers) == API_KEY_BEARER_ID
    body = b'data: {"type":"message_delta","usage":{"input_tokens":1000000,"output_tokens":0}}\n'
    proxy._record_usage(
        MODEL, MODEL, bytearray(body), "v1/messages", seat=API_KEY_BEARER_ID, seat_headers=headers
    )
    spent = metered_spend.window_spend(config.API_KEY_SPEND_FILE, WINDOW, NOW)
    assert (spent > 0) is recorded


@pytest.mark.parametrize("content", ["{", json.dumps({"entries": [[1, -1]]}), "[]"])
def test_unreadable_spend_fails_closed_and_is_never_overwritten(funded, logs, content):
    path = config.API_KEY_SPEND_FILE
    with open(path, "w") as handle:
        handle.write(content)
    assert proxy._api_key_candidate(MODEL) is None
    assert not metered_spend.record(path, 1.0, WINDOW, NOW)
    with open(path) as handle:
        assert handle.read() == content


def test_unpriced_model_refused(funded, logs):
    assert proxy._api_key_candidate("model-with-no-price") is None
    assert logs == ["api-key-candidate refused reason=unpriced-model"]


async def test_health_reports_ceiling_and_window_spend(funded, monkeypatch):
    # health() reads process state the gateway seeds at import; an earlier test
    # in the full suite can leave it without this key (CI run 351 KeyError).
    monkeypatch.setitem(proxy.state, "upstream_auth_ok", True)
    spend(2.5)
    body = json.loads((await proxy.health(None)).body)["api_key"]
    assert body["usd_ceiling"] == 10.0
    assert body["usd_window_s"] == WINDOW
    assert body["usd_window_spend"] == 2.5
