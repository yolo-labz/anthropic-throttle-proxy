"""Balanced round-robin pool: routing seam + accounting, offline (no provider I/O).

Drives the real ``_route_account_if_enabled`` seam with a two-account credential
fixture, then settles each attempt through ``BALANCER.begin``/``finish`` exactly
as ``forwarding._forward_once`` does. No generation request is ever sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import pytest

from anthropic_throttle_proxy import account_pool, accounts, config, proxy


def _write_cred(path: Any, token: str) -> str:
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return hashlib.sha256(f"Bearer {token}".encode()).hexdigest()[:8]


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setattr(config, "API_KEY_FILE", "")
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "off")
    monkeypatch.setattr(proxy, "_api_key_cache", None)
    cred_a = tmp_path / "a.json"
    cred_b = tmp_path / "b.json"
    bid_a = _write_cred(cred_a, "sk-ant-oat01-SIM-A")
    bid_b = _write_cred(cred_b, "sk-ant-oat01-SIM-B")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred_a},B:{cred_b}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "round_robin")
    monkeypatch.setattr(account_pool, "BALANCER", account_pool.BalancedRoundRobin())
    monkeypatch.setattr(proxy, "_bearer_credential_dead", lambda bid: False)
    monkeypatch.setattr(proxy, "_bearer_retry_after_remaining", lambda bid: 0.0)
    return {"A": bid_a, "B": bid_b}


def _route(bids: dict[str, str], incoming: str | None = None) -> tuple[str, str | None]:
    headers = {"authorization": "Bearer sk-ant-oat01-SIM-A", "x-test": "1"}
    selected, label = proxy._route_account_if_enabled(
        headers, incoming or bids["A"], method="POST", path="v1/messages", max_tokens=64
    )
    return label, headers.get("Authorization") if label else None


def _attempt(bids: dict[str, str], usage: dict | None) -> str:
    label, _ = _route(bids)
    ticket = account_pool.BALANCER.begin(bids[label])
    account_pool.BALANCER.finish(ticket, usage)
    return label


def _rows(bids: dict[str, str]) -> dict[str, dict]:
    return {
        row["label"]: row for row in account_pool.BALANCER.rows({"A": bids["A"], "B": bids["B"]})
    }


def test_read_only_polls_do_not_advance_alternation(pool) -> None:
    """Status polls between requests must not consume a turn."""
    labels = []
    for _ in range(6):
        # Polls: the pure selection seam and the dashboard row projection.
        before = account_pool.BALANCER.rows({"A": pool["A"], "B": pool["B"]})
        snapshot = accounts.routing_snapshot()
        proxy._account_selection(snapshot, pool["A"], now=0.0, allow_retry_probe=False)
        proxy._round_robin_candidates(
            snapshot, model="", max_tokens=None, now=0.0, allow_retry_probe=False
        )
        assert account_pool.BALANCER.rows({"A": pool["A"], "B": pool["B"]}) == before
        labels.append(_attempt(pool, {"input": 10, "output": 10}))
    assert labels == ["A", "B", "A", "B", "A", "B"]


def test_large_responses_are_compensated_by_observed_tokens(pool) -> None:
    """100-token responses on A vs 520-token responses on B: token work converges."""
    counts = {"A": 0, "B": 0}
    for _ in range(60):
        label, _ = _route(pool)
        usage = {"input": 50, "output": 50} if label == "A" else {"input": 400, "output": 120}
        ticket = account_pool.BALANCER.begin(pool[label])
        account_pool.BALANCER.finish(ticket, usage)
        counts[label] += 1
    rows = _rows(pool)
    assert counts["A"] > counts["B"]
    assert abs(rows["A"]["tokens"] - rows["B"]["tokens"]) <= 520


def test_four_concurrent_requests_split_two_per_account(pool) -> None:
    """In-flight reservations, not completions, drive the split."""
    tickets, labels = [], []
    for _ in range(4):
        label, _ = _route(pool)
        labels.append(label)
        tickets.append(account_pool.BALANCER.begin(pool[label]))
    assert sorted(labels) == ["A", "A", "B", "B"]
    assert labels[0] != labels[1]
    for ticket in tickets:
        account_pool.BALANCER.finish(ticket, {"input": 10, "output": 10})


def test_held_and_dead_accounts_are_excluded(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        proxy, "_bearer_retry_after_remaining", lambda bid: 3600.0 if bid == pool["A"] else 0.0
    )
    assert {_route(pool)[0] for _ in range(3)} == {"B"}
    monkeypatch.setattr(proxy, "_bearer_retry_after_remaining", lambda bid: 0.0)
    monkeypatch.setattr(proxy, "_bearer_credential_dead", lambda bid: bid == pool["B"])
    assert {_route(pool)[0] for _ in range(3)} == {"A"}


def test_caller_api_key_bypasses_rotation(pool) -> None:
    headers = {"x-api-key": "sk-ant-api03-CLIENT"}
    selected, label = proxy._route_account_if_enabled(
        headers, pool["A"], method="POST", path="v1/messages"
    )
    assert label is None and selected == pool["A"]
    assert headers == {"x-api-key": "sk-ant-api03-CLIENT"}
    assert account_pool.BALANCER.rows({"A": pool["A"], "B": pool["B"]})[0]["attempts"] == 0


def test_cancelled_pacing_does_not_debit(pool) -> None:
    label, _ = _route(pool)
    before = account_pool.BALANCER.key(label)
    ticket = account_pool.BALANCER.begin(pool[label])
    account_pool.BALANCER.finish(ticket, None, discarded=True)
    assert account_pool.BALANCER.key(label) == before
    row = _rows(pool)[label]
    assert row["attempts"] == 0 and row["tokens"] == 0 and row["estimated"] == 0
    assert row["pending_tokens"] == 0


def test_unknown_sent_outcome_is_estimated_not_free(pool) -> None:
    label = _attempt(pool, None)
    assert label == "A"
    # B settles with 500 measured tokens; A's unknown send must still weigh its estimate.
    ticket = account_pool.BALANCER.begin(pool["B"])
    account_pool.BALANCER.finish(ticket, {"input": 500})
    assert _route(pool)[0] == "B"
    row = _rows(pool)[label]
    assert row["attempts"] == 1 and row["estimated"] == 1 and row["tokens"] == 0
    assert row["token_share"] == 0.0  # observed tokens exist elsewhere; A's share is 0 of 500


def test_empty_history_is_unknown_not_zero(pool) -> None:
    rows = account_pool.BALANCER.rows({"A": pool["A"], "B": pool["B"]})
    assert all(row["request_share"] is None and row["token_share"] is None for row in rows)
    assert all(row["attempts"] == 0 for row in rows)


# ── Integration acceptance (366): cancellation, concurrency, rejoin, unknown usage ──
#
# The pool is not a semaphore: a "slot" is a ticket whose estimate sits in
# ``pending`` from ``begin`` (just before the upstream send) until ``finish``
# (the send's ``finally`` in ``forwarding._forward_once``). The real concurrency
# CAP stays the per-bearer ``FairBearerLimiter``; the pool only decides WHERE.
# These tests therefore assert the pool's own invariants: no reservation
# outlives its request, no ticket settles twice, and the split follows work.


async def _held_send(bids: dict[str, str], gate: asyncio.Event, out: list) -> None:
    """Mirror ``_forward_once``: route, begin, await the send, finish in ``finally``."""
    label, _ = _route(bids)
    ticket = account_pool.BALANCER.begin(bids[label])
    out.append((label, ticket))
    usage = None
    try:
        await gate.wait()
        usage = {"input": 10, "output": 10}
    finally:
        account_pool.BALANCER.finish(ticket, usage)


async def test_cancelled_while_holding_a_slot_returns_it_without_leak(pool) -> None:
    """Req 1: a request cancelled mid-flight releases its reservation."""
    gate, held = asyncio.Event(), []
    task = asyncio.create_task(_held_send(pool, gate, held))
    await asyncio.sleep(0)
    ((label, ticket),) = held
    assert _rows(pool)[label]["pending_tokens"] == ticket.estimate > 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = _rows(pool)[label]
    assert row["pending_tokens"] == 0  # reservation returned
    # The request may already have reached upstream: charged its estimate, never free.
    assert row["attempts"] == 1 and row["estimated"] == 1 and row["tokens"] == 0
    account_pool.BALANCER.finish(ticket, {"input": 999})  # late settle is a no-op
    assert _rows(pool)[label]["pending_tokens"] == 0 and _rows(pool)[label]["tokens"] == 0
    # Nothing is stuck: the charged estimate only makes the other account catch
    # up first; once it has done equal work the cancelled account serves again.
    labels = [_attempt(pool, {"input": ticket.estimate}) for _ in range(3)]
    assert label in labels and len(set(labels)) == 2


async def test_concurrent_acquires_never_double_issue_or_overcommit(pool) -> None:
    """Req 2: N concurrent requests get N distinct tickets, split evenly, all returned."""
    n = 20
    gate, held = asyncio.Event(), []
    tasks = [asyncio.create_task(_held_send(pool, gate, held)) for _ in range(n)]
    await asyncio.sleep(0)
    assert len(held) == n
    assert len({id(ticket) for _, ticket in held}) == n  # no ticket issued twice
    rows = _rows(pool)
    pending = sum(row["pending_tokens"] for row in rows.values())
    assert pending == sum(ticket.estimate for _, ticket in held)  # exactly N reservations
    counts = {k: sum(1 for label, _ in held if label == k) for k in ("A", "B")}
    assert counts == {"A": n // 2, "B": n // 2}  # in-flight work, not completions, drives it
    gate.set()
    await asyncio.gather(*tasks)
    rows = _rows(pool)
    assert all(row["pending_tokens"] == 0 for row in rows.values())
    assert sum(row["attempts"] for row in rows.values()) == n
    assert all(ticket.done for _, ticket in held)


def test_recovered_account_rejoins_without_restart_or_backlog_burst(pool) -> None:
    """Req 3: a rejected account returns as soon as its state clears, at the floor."""
    now = time.time()
    config.bearer_state[pool["A"]] = {
        "unified": {"status": "rejected", "util_5h": 1.0, "reset_5h": now + 3600}
    }
    assert [_attempt(pool, {"input": 100}) for _ in range(5)] == ["B"] * 5
    # Fresh headers report the window allowed again (what _apply_unified writes).
    config.bearer_state[pool["A"]] = {
        "unified": {"status": "allowed", "util_5h": 0.2, "reset_5h": now + 3600}
    }
    labels = [_attempt(pool, {"input": 100}) for _ in range(4)]
    assert labels[0] == "A"  # eligible again, same process, no restart
    # Rejoins at the floor: it is NOT owed the 5 requests B served meanwhile.
    assert sorted(labels) == ["A", "A", "B", "B"]


def test_unknown_usage_account_is_neither_preferred_nor_excluded(pool) -> None:
    """Req 4: no unified state => eligible, ranked only by the pool's own token work.

    Chosen behaviour: an account whose unified usage is unknown stays ELIGIBLE
    (excluding it would strand a healthy account whose headers simply have not
    arrived yet, e.g. right after start) but gets NO preference for its missing
    utilisation — round_robin ranks by observed token work, never by util, so
    "unknown" cannot read as "0 % used". Hard gates (retry-after, dead
    credential, rejected) still exclude it as they would any account.
    """
    now = time.time()
    config.bearer_state[pool["A"]] = {
        "unified": {"status": "allowed", "util_5h": 0.1, "util_7d": 0.2, "reset_5h": now + 3600}
    }
    assert pool["B"] not in config.bearer_state  # B: no usable unified state
    labels = [_attempt(pool, {"input": 50, "output": 50}) for _ in range(8)]
    assert sorted(labels) == ["A"] * 4 + ["B"] * 4
    # A malformed unified blob is likewise neither a preference nor an exclusion.
    config.bearer_state[pool["B"]] = {"unified": {"status": None}}
    labels = [_attempt(pool, {"input": 50, "output": 50}) for _ in range(4)]
    assert sorted(labels) == ["A", "A", "B", "B"]
