"""A request parked behind an account that closed for hours moves to a sibling.

13/11/2026 07:04 BRT: ten requests queued on the gmail account at 11/11 18:15,
when it hit its weekly limit, were still parked 37 h later (1 in flight, 9
queued, Retry-After to 13/11 18:00) while the proton account had reset at 07:00
and served new traffic with up to 20 slots. Real proxy app, two real account
credentials, recording upstream (``pool`` from test_seat_capacity_acceptance).
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
import test_seat_capacity_acceptance as _seats
from test_proxy_app import _wait_for_limiter_queued
from test_seat_capacity_acceptance import _open_seat, _post, _seat_of, _settle

from anthropic_throttle_proxy import accounts, config, limiter, proxy
from anthropic_throttle_proxy import session_affinity as sa

pool = _seats.pool  # two accounts A/B, real handler, recording upstream


async def _park_on_a(pool):
    """A's only slot is busy upstream, B is closed, X queues on A."""
    lim_a = await _open_seat(pool, "A", live=1)
    lim_b = await _open_seat(pool, "B", live=1)
    lim_b.note_retry_after(30)  # B closed when X arrives
    busy = asyncio.create_task(_post(pool.client, tag="busy", hold=True))
    await pool.upstream.wait_arrived("busy")
    parked = asyncio.create_task(_post(pool.client, tag="x"))
    await _wait_for_limiter_queued(lim_a, 1)
    return lim_a, lim_b, busy, parked


async def test_parked_request_moves_to_a_reopened_sibling(pool) -> None:
    lim_a, lim_b, busy, parked = await _park_on_a(pool)
    # A hits its weekly limit (the 429 aftermath); B's window ends.
    lim_a.note_retry_after(10 * 3600)
    limiter.require_retry_probe(pool.bids["A"], block_while_retry=True)
    lim_b._retry_after_until = 0.0

    status, body = await asyncio.wait_for(parked, 5)  # no client action
    assert (status, _seat_of(body)) == (200, "B")
    assert [tag for _seat, tag in pool.upstream.order].count("x") == 1

    pool.upstream.let_go("busy")
    await asyncio.wait_for(busy, 5)
    await _settle()
    for lim in (lim_a, lim_b):
        assert lim.inflight == 0 and lim.queued_total == 0


@pytest.fixture
def lines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = proxy.log

    def record(message: str) -> None:
        seen.append(message)
        real(message)

    monkeypatch.setattr(proxy, "log", record)
    return seen


async def _close_a(pool, lim_b, *, b_reopens: bool) -> None:
    pool_a = config.bearer_limiters[pool.bids["A"]]
    pool_a.note_retry_after(10 * 3600)
    limiter.require_retry_probe(pool.bids["A"], block_while_retry=True)
    if b_reopens:
        lim_b._retry_after_until = 0.0
    else:
        lim_b.note_retry_after(11 * 3600)  # B stays closed even longer


async def _drain(pool, busy, *lims) -> None:
    pool.upstream.let_go("busy")
    await asyncio.wait_for(busy, 5)
    await _settle()
    for lim in lims:
        assert lim.inflight == 0 and lim.queued_total == 0 and lim._holds == {}


async def test_the_move_follows_the_pin_and_logs_why(pool, lines) -> None:
    lim_a, lim_b, busy, parked = await _park_on_a(pool)
    migrated = sa.stats()["migrated"]
    await _close_a(pool, lim_b, b_reopens=True)
    status, body = await asyncio.wait_for(parked, 5)
    assert (status, _seat_of(body)) == (200, "B")
    a, b = pool.bids["A"], pool.bids["B"]
    moved = [line for line in lines if line.startswith("parked-reroute ")]
    assert len(moved) == 1 and f"from={a}" in moved[0]
    assert "reason=account_closed_past_hold_bound" in moved[0]
    assert any(
        line.startswith("affinity-migrate ") and f"from={a} to={b}" in line for line in lines
    )
    assert sa.stats()["migrated"] == migrated + 1
    await _drain(pool, busy, lim_a, lim_b)


async def test_nothing_moves_when_the_sibling_is_closed_too(pool, lines) -> None:
    lim_a, lim_b, busy, parked = await _park_on_a(pool)
    await _close_a(pool, lim_b, b_reopens=False)
    await asyncio.sleep(2.5)  # two leave polls
    assert not parked.done()
    assert lim_a.queued_total == 1
    assert not any(line.startswith("parked-reroute ") for line in lines)
    parked.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await parked
    await _drain(pool, busy, lim_a, lim_b)
    assert pool.upstream.count.get("B", 0) == 0


async def test_a_request_already_upstream_is_never_sent_twice(pool) -> None:
    lim_a, lim_b, busy, parked = await _park_on_a(pool)
    await _close_a(pool, lim_b, b_reopens=True)
    await asyncio.wait_for(parked, 5)
    await _drain(pool, busy, lim_a, lim_b)
    tags = [tag for _seat, tag in pool.upstream.order]
    assert tags.count("busy") == 1 and tags.count("x") == 1
    assert ("A", "busy") in pool.upstream.order


async def test_a_single_account_setup_waits_as_before(pool, monkeypatch, lines) -> None:
    lim_a = await _open_seat(pool, "A", live=1)
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']}")
    accounts._cache.clear()
    busy = asyncio.create_task(_post(pool.client, tag="busy", hold=True))
    await pool.upstream.wait_arrived("busy")
    parked = asyncio.create_task(_post(pool.client, tag="x"))
    await _wait_for_limiter_queued(lim_a, 1)
    lim_a.note_retry_after(10 * 3600)
    assert proxy._parked_closed_for(pool.bids["A"], "", model="", max_tokens=None) is None
    await asyncio.sleep(1.5)
    assert not parked.done() and lim_a.queued_total == 1
    parked.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await parked
    await _drain(pool, busy, lim_a)
    assert not any(line.startswith("parked-reroute ") for line in lines)


def test_api_key_and_off_routing_never_match(pool, monkeypatch) -> None:
    """The predicate only answers for an account-pool bearer with a sibling."""
    a = pool.bids["A"]
    config.bearer_limiters.pop(a, None)
    limiter._retry_after_state = {a: time.time() + 10 * 3600}
    # A bearer outside the pool (an API key, an unrouted client) never moves.
    assert proxy._parked_closed_for("api-key", "", model="", max_tokens=None) is None
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "off")
    assert proxy._parked_closed_for(a, "", model="", max_tokens=None) is None


async def test_a_short_window_stays_parked(pool, monkeypatch) -> None:
    """Below MAX_HOLD_RETRY_AFTER_S the request keeps its queue position."""
    lim_a, lim_b, busy, parked = await _park_on_a(pool)
    lim_a.note_retry_after(config.MAX_HOLD_RETRY_AFTER_S / 2)
    lim_b._retry_after_until = 0.0
    assert proxy._parked_closed_for(pool.bids["A"], "", model="", max_tokens=None) is None
    pool.upstream.let_go("busy")
    pool.upstream.let_go("x")
    await asyncio.wait_for(busy, 5)
    lim_a._retry_after_until = 0.0  # the short window ends; X dispatches on A
    status, body = await asyncio.wait_for(parked, 5)
    assert (status, _seat_of(body)) == (200, "A")
