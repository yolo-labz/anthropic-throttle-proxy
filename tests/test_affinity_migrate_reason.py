"""The ``affinity-migrate`` log line names why a warm pin was released (log-only)."""

from __future__ import annotations

import copy
import re
import time

import pytest
import test_session_affinity as _sibling
from test_session_affinity import _body, _route

from anthropic_throttle_proxy import account_pool, accounts, proxy
from anthropic_throttle_proxy import session_affinity as sa

pool = _sibling.pool  # the two-account fixture, under both routing modes
LINE = re.compile(r"^affinity-migrate session=\S+ from=(\S+) to=(\S+) reason=(\S+)$")


@pytest.fixture
def lines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(proxy, "log", lambda msg, *a, **k: captured.append(str(msg)))
    return captured


def _migrate(pool, lines, monkeypatch, release) -> tuple[str, str, str]:
    body = _body()
    first = _route(pool, body)
    other = "B" if first == "A" else "A"
    release(monkeypatch, pool, first, other)
    assert _route(pool, body) == other
    found = [m for m in map(LINE.match, lines) if m]
    assert len(found) == 1, lines
    old, new, reason = found[0].groups()
    assert (old, new) == (pool[first], pool[other])  # existing fields keep their order
    return old, new, reason


def _retry_after(mp, bids, first, _other) -> None:
    mp.setattr(proxy, "_bearer_retry_after_remaining", lambda b: 60.0 if b == bids[first] else 0.0)


def _model_scoped_retry_after(mp, bids, first, _other) -> None:
    # #46: a model-scoped 429 window leaves the bearer-wide Retry-After closed.
    mp.setattr(
        proxy._model_retry,
        "scoped_remaining",
        lambda b, model="": 60.0 if b == bids[first] else 0.0,
    )


def _strict_gate(mp, bids, first, _other) -> None:
    mp.setattr(proxy, "_bearer_credential_dead", lambda b: b == bids[first])


def _not_routable(mp, bids, first, _other) -> None:
    real = accounts.routing_snapshot
    mp.setattr(
        accounts,
        "routing_snapshot",
        lambda now=None: [a for a in real(now) if a.get("bearer_id") != bids[first]],
    )


def _would_queue(mp, bids, first, _other) -> None:
    # As in test_session_affinity: a queued pinned account also scores as loaded.
    mp.setattr(proxy, "_bearer_would_queue", lambda b: b == bids[first])
    mp.setattr(proxy, "_bearer_local_load_score", lambda b: 100.0 if b == bids[first] else 0.0)


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        (_retry_after, "retry_after"),
        (_model_scoped_retry_after, "retry_after"),
        (_strict_gate, "strict_gate"),
        (_not_routable, "not_routable"),
        (_would_queue, "would_queue"),
    ],
)
def test_each_release_is_named(pool, lines, monkeypatch, release, expected) -> None:
    assert _migrate(pool, lines, monkeypatch, release)[2] == expected


def test_a_move_the_pin_verdict_cannot_explain_is_unknown(pool, lines) -> None:
    snapshot = [{"bearer_id": pool["A"]}, {"bearer_id": pool["B"]}]
    sa.pin("k-unknown", pool["A"], now=time.time())
    proxy._remember_session_account(snapshot, "k-unknown", {"bearer_id": pool["B"]}, time.time())
    assert [m.group(3) for m in map(LINE.match, lines) if m] == ["unknown"]


def test_kept_session_logs_nothing(pool, lines) -> None:
    body = _body()
    _route(pool, body)
    _route(pool, body)
    assert not [line for line in lines if line.startswith("affinity-migrate")]


@pytest.mark.parametrize(
    "release", [_retry_after, _model_scoped_retry_after, _strict_gate, _not_routable, _would_queue]
)
def test_reason_helper_never_changes_the_chosen_account(pool, monkeypatch, release) -> None:
    """Same route with the reason helper live and with it replaced by a constant."""
    body = _body()
    first = _route(pool, body)
    other = "B" if first == "A" else "A"
    release(monkeypatch, pool, first, other)
    balancer = copy.deepcopy(account_pool.BALANCER)  # replay from identical state
    with_helper = _route(pool, body)
    monkeypatch.setattr(account_pool, "BALANCER", balancer)
    sa.reset()
    key = sa.begin_request(body)
    assert key is not None
    sa.pin(key, pool[first], now=time.time())
    calls: list[int] = []
    real = proxy._remember_session_account

    def no_helper(snapshot, key, selected, now, release_reason=None):
        calls.append(1)
        return real(snapshot, key, selected, now, lambda: "unknown")

    monkeypatch.setattr(proxy, "_remember_session_account", no_helper)
    assert _route(pool, body) == with_helper == other
    assert calls
