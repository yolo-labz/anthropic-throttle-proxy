"""TD-B0a: a session stays on the account it started on; new sessions are still paced.

Drives the real ``_route_account_if_enabled`` seam with two offline credentials
(no provider I/O), like ``test_account_pool_integration``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_account_pool_integration import _write_cred

from anthropic_throttle_proxy import account_pool, accounts, config, proxy
from anthropic_throttle_proxy import session_affinity as sa


def _body(system: str = "sys", first: str = "hello", model: str = "claude-sonnet-5-5") -> bytes:
    return json.dumps(
        {"model": model, "system": system, "messages": [{"role": "user", "content": first}]}
    ).encode()


@pytest.fixture(params=["round_robin", "budget_paced"])
def pool(request, monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    for cache in (
        accounts._cache,
        accounts._endpoint_cache,
        config.bearer_limiters,
        config.bearer_state,
    ):
        cache.clear()
    sa.reset()
    for name, value in (("API_KEY_FILE", ""), ("API_KEY_ROUTING_MODE", "off")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(proxy, "_api_key_cache", None)
    paths = {label: tmp_path / f"{label}.json" for label in "AB"}
    bids = {label: _write_cred(path, f"sk-ant-oat01-SIM-{label}") for label, path in paths.items()}
    monkeypatch.setattr(
        config, "ACCOUNT_CRED_PATHS", ",".join(f"{k}:{v}" for k, v in paths.items())
    )
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", request.param)
    monkeypatch.setattr(account_pool, "BALANCER", account_pool.BalancedRoundRobin())
    monkeypatch.setattr(proxy, "_bearer_credential_dead", lambda bid: False)
    monkeypatch.setattr(proxy, "_bearer_retry_after_remaining", lambda bid: 0.0)
    yield bids
    sa.reset()
    sa._current.set(None)


def _route(bids: dict[str, str], body: bytes | None) -> str:
    sa.begin_request(body)
    headers = {"authorization": "Bearer sk-ant-oat01-SIM-A"}
    _bid, label = proxy._route_account_if_enabled(
        headers, bids["A"], method="POST", path="v1/messages", max_tokens=64
    )
    assert label is not None
    return label


def _settle(bids: dict[str, str], label: str) -> None:
    ticket = account_pool.BALANCER.begin(bids[label])
    account_pool.BALANCER.finish(ticket, {"input": 10, "output": 10})


def test_same_session_stays_put(pool) -> None:
    body = _body()
    first = _route(pool, body)
    labels = []
    for _ in range(8):
        _settle(pool, first)
        labels.append(_route(pool, body))
    assert set(labels) == {first}
    assert sa.stats()["kept"] == 8 and sa.stats()["migrated"] == 0


@pytest.mark.parametrize("reason", ["retry_after", "dead", "warn"])
def test_pinned_session_migrates_when_account_unusable(
    pool, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    body = _body()
    first = _route(pool, body)
    other = "B" if first == "A" else "A"
    bad = pool[first]
    if reason == "retry_after":  # 429 / brake both land as a Retry-After window
        monkeypatch.setattr(
            proxy, "_bearer_retry_after_remaining", lambda bid: 60.0 if bid == bad else 0.0
        )
    elif reason == "dead":
        monkeypatch.setattr(proxy, "_bearer_credential_dead", lambda bid: bid == bad)
    else:  # util >= warn: strict gate refuses, the account is only a pressured fallback
        config.bearer_state[bad] = {
            "unified": {"status": "allowed_warning", "util_5h": 0.9, "reset_5h": 9e12}
        }
    assert _route(pool, body) == other
    # ...and it stays on the new account once the old one recovers (no flip back).
    monkeypatch.setattr(proxy, "_bearer_retry_after_remaining", lambda bid: 0.0)
    monkeypatch.setattr(proxy, "_bearer_credential_dead", lambda bid: False)
    config.bearer_state.pop(bad, None)
    assert _route(pool, body) == other
    assert sa.stats()["migrated"] == 1


def test_queue_on_pinned_account_releases_the_pin_only_when_a_sibling_has_room(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _body()
    first = _route(pool, body)
    other = "B" if first == "A" else "A"
    busy = pool[first]
    loads = {pool["A"]: 0.0, pool["B"]: 0.0}
    monkeypatch.setattr(proxy, "_bearer_local_load_score", lambda bid: loads[bid])
    loads[busy] = 100.0  # a request is queued behind the pinned account
    assert _route(pool, body) == other
    loads[pool[other]] = 100.0  # now the new home is queued too: nothing to gain by hopping
    assert _route(pool, body) == other


def test_new_sessions_are_still_balanced(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """#370 fairness decides where NEW sessions start; each then stays."""
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "round_robin")  # the #370 placement policy
    homes = {}
    for i in range(6):
        body = _body(first=f"session {i}")
        homes[i] = _route(pool, body)
        _settle(pool, homes[i])
    assert {homes[i] for i in homes} == {"A", "B"}
    for i in homes:
        assert _route(pool, _body(first=f"session {i}")) == homes[i]


def test_no_key_means_no_affinity(pool) -> None:
    labels = {_route(pool, None) for _ in range(4)}
    assert sa.stats()["new"] == 0 and labels  # unkeyed requests are routed exactly as before


def test_kill_switch(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SESSION_AFFINITY", "off")
    assert sa.begin_request(_body()) is None


def test_key_derivation() -> None:
    assert sa.derive_key(_body()) == sa.derive_key(_body())
    assert sa.derive_key(_body()) != sa.derive_key(_body(first="other"))
    assert sa.derive_key(_body()) != sa.derive_key(_body(system="other"))
    assert sa.derive_key(_body()) != sa.derive_key(_body(model="claude-opus-5-5"))
    uid = json.dumps({"metadata": {"user_id": "u1"}, "messages": []}).encode()
    assert sa.derive_key(uid) == sa.derive_key(
        uid.replace(b"[]", b'[{"role":"user","content":"x"}]')
    )
    assert sa.derive_key(b"") is None and sa.derive_key(b"not json") is None
    assert sa.derive_key(b'{"messages":[]}') is None
    assert "hello" not in (sa.derive_key(_body()) or "")


def test_pin_ttl_and_bound() -> None:
    sa.reset()
    sa.pin("k", "bid", now=0.0)
    assert sa.pinned("k", now=sa.TTL_S - 1) == "bid"
    assert sa.pinned("k", now=sa.TTL_S + 2) is None
    for i in range(sa.MAX_PINS + 5):
        sa.pin(f"k{i}", "b", now=1.0)
    assert sa.stats()["pins"] == sa.MAX_PINS
    sa.reset()
