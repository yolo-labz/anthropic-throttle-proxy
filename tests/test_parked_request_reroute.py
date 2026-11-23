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
import json
import time
import uuid

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
    """No sibling can take it: it is ended (terminal below), never moved or sent."""
    lim_a, lim_b, busy, parked = await _park_on_a(pool)
    await _close_a(pool, lim_b, b_reopens=False)
    status, body = await asyncio.wait_for(parked, 5)
    assert status == 429 and b"closed for" in body
    assert not any(line.startswith("parked-reroute ") for line in lines)
    await _drain(pool, busy, lim_a, lim_b)
    assert pool.upstream.count.get("B", 0) == 0
    assert [tag for _seat, tag in pool.upstream.order].count("x") == 0


async def test_a_request_already_upstream_is_never_sent_twice(pool) -> None:
    lim_a, lim_b, busy, parked = await _park_on_a(pool)
    await _close_a(pool, lim_b, b_reopens=True)
    await asyncio.wait_for(parked, 5)
    await _drain(pool, busy, lim_a, lim_b)
    tags = [tag for _seat, tag in pool.upstream.order]
    assert tags.count("busy") == 1 and tags.count("x") == 1
    assert ("A", "busy") in pool.upstream.order


async def test_a_single_account_setup_is_ended_like_a_new_arrival(pool, monkeypatch, lines) -> None:
    """No sibling exists, so there is nothing to move to: the parked request is
    ended with the answer a new arrival gets (it was held forever before)."""
    lim_a = await _open_seat(pool, "A", live=1)
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{pool.key_files['A']}")
    accounts._cache.clear()
    busy = asyncio.create_task(_post(pool.client, tag="busy", hold=True))
    await pool.upstream.wait_arrived("busy")
    parked = asyncio.create_task(_post(pool.client, tag="x"))
    await _wait_for_limiter_queued(lim_a, 1)
    lim_a.note_retry_after(10 * 3600)
    assert proxy._parked_closed_for(pool.bids["A"], "", model="", max_tokens=None) is None
    status, _body = await asyncio.wait_for(parked, 5)
    assert status == 429
    await _drain(pool, busy, lim_a)
    assert not any(line.startswith("parked-reroute ") for line in lines)
    assert any(line.startswith("parked-terminal ") for line in lines)


def test_api_key_and_off_routing_never_match(pool, monkeypatch) -> None:
    """The predicate only answers for an account-pool bearer with a sibling."""
    a = pool.bids["A"]
    config.bearer_limiters.pop(a, None)
    limiter._retry_after_state = {a: time.time() + 10 * 3600}
    # A bearer outside the pool (an API key, an unrouted client) never moves.
    assert proxy._parked_closed_for("api-key", "", model="", max_tokens=None) is None
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "off")
    assert proxy._parked_closed_for(a, "", model="", max_tokens=None) is None


def test_the_sibling_scan_runs_once_per_account_per_second(pool, monkeypatch) -> None:
    """p2S S1: 200 parked requests on one closed account cost one snapshot scan."""
    a = pool.bids["A"]
    config.bearer_limiters.pop(a, None)
    limiter._retry_after_state = {a: time.time() + 10 * 3600}
    scans: list[str] = []
    real = proxy._parked_sibling_open

    def counting(*args, **kwargs):
        scans.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(proxy, "_parked_sibling_open", counting)
    verdicts = [proxy._parked_closed_for(a, "", model="m", max_tokens=8) for _ in range(200)]
    assert None not in verdicts
    assert scans == [a]


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


async def test_cancel_after_dispatch_does_not_leak_the_slot() -> None:
    """p2S adversarial read of #76, D1: release() hands the parked acquire a
    slot and the client cancels in the same loop turn; the slot must come back."""
    lim = limiter.FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    await lim.acquire("holder")

    async def parked() -> None:
        async with lim.slot("waiter", leave_when=lambda: False, leave_poll_s=10.0):
            await asyncio.sleep(10)

    outer = asyncio.create_task(parked())
    for _ in range(50):
        if lim.snapshot()["queued_total"] == 1:
            break
        await asyncio.sleep(0.001)
    assert lim.snapshot()["queued_total"] == 1
    await lim.release()
    outer.cancel()
    await asyncio.gather(outer, return_exceptions=True)
    await asyncio.sleep(0)
    snap = lim.snapshot()
    assert snap["inflight"] == 0, f"slot leaked: {snap['inflight']} inflight after cancel"
    assert snap["queued_total"] == 0


async def test_an_unused_finished_acquire_is_always_given_back() -> None:
    """Every exit (cancel, leave, timeout) abandons through one helper; a finished
    acquire it is handed is released, a parked one is rolled back."""
    lim = limiter.FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    context = lim.slot("w", leave_when=lambda: False)
    finished = asyncio.ensure_future(lim.acquire_lease("w"))
    await finished
    assert lim.snapshot()["inflight"] == 1
    await context._abandon(finished)
    assert lim.snapshot()["inflight"] == 0
    await lim.acquire("holder")
    parked_acquire = asyncio.ensure_future(lim.acquire_lease("w"))
    await asyncio.sleep(0)
    assert lim.snapshot()["queued_total"] == 1
    await context._abandon(parked_acquire)
    snap = lim.snapshot()
    assert (snap["inflight"], snap["queued_total"]) == (1, 0)
    await lim.release()
    assert lim.snapshot()["inflight"] == 0


async def test_leaving_with_the_probe_frees_it_for_the_next_cycle(pool) -> None:
    """p2S S2: X owns A's half-open probe while parked, then leaves. The probe
    must be free at once (closed, still required), not one extra cycle later."""
    lim_a = await _open_seat(pool, "A", live=1)
    lim_b = await _open_seat(pool, "B", live=1)
    a = pool.bids["A"]
    lim_b.note_retry_after(30)
    busy = asyncio.create_task(_post(pool.client, tag="busy", hold=True))
    await pool.upstream.wait_arrived("busy")
    limiter.require_retry_probe(a)  # A half-open: the next message is its probe
    parked = asyncio.create_task(_post(pool.client, tag="x"))
    await _wait_for_limiter_queued(lim_a, 1)
    assert limiter.retry_probe_inflight(a), "X claimed A's probe before parking"

    lim_a.note_retry_after(10 * 3600)
    lim_b._retry_after_until = 0.0
    status, body = await asyncio.wait_for(parked, 5)
    assert (status, _seat_of(body)) == (200, "B")

    assert not limiter.retry_probe_inflight(a)
    assert limiter.retry_probe_required(a)  # never reopened untried
    lim_a._retry_after_until = 0.0  # A's window ends
    assert limiter.try_begin_retry_probe(a) is True  # no wait for a stale lease
    limiter.finish_retry_probe(a, success=True)
    await _drain(pool, busy, lim_a, lim_b)


# --- parked-no-sibling-terminal (13/11/2026): ended when no sibling can take it ---

_INCIDENT_RETRY_AFTER_S = 171837  # 11/11 18:15:56: the 429 that parked a Pi turn 38.6 h


async def _post_raw(pool, tag: str, **body_extra):
    """One request with the real headers; returns (status, headers, payload)."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer client-{tag}-{uuid.uuid4().hex}",
        "X-Test-Tag": tag,
    }
    headers.update(body_extra.pop("headers", {}))
    body = {**_seats._BODY, "metadata": {"user_id": f"session-{tag}"}, **body_extra}
    response = await asyncio.wait_for(
        pool.client.post("/v1/messages", headers=headers, json=body), 8
    )
    payload = await asyncio.wait_for(response.read(), 8)
    return response.status, response.headers, payload


async def _park_no_sibling(pool, **extra):
    """A busy, B closed for good, X parks on A; then A gets the incident window."""
    lim_a = await _open_seat(pool, "A", live=1)
    lim_b = await _open_seat(pool, "B", live=1)
    lim_b.note_retry_after(_INCIDENT_RETRY_AFTER_S + 60)
    busy = asyncio.create_task(_post(pool.client, tag="busy", hold=True))
    await pool.upstream.wait_arrived("busy")
    parked = asyncio.create_task(_post_raw(pool, "x", **extra))
    await _wait_for_limiter_queued(lim_a, 1)
    lim_a.note_retry_after(_INCIDENT_RETRY_AFTER_S)
    return lim_a, lim_b, busy, parked


async def test_parked_with_no_sibling_gets_the_terminal_within_one_poll(pool, lines) -> None:
    lim_a, lim_b, busy, parked = await _park_no_sibling(pool)
    started = time.monotonic()
    status, headers, body = await asyncio.wait_for(parked, 6)
    assert time.monotonic() - started < 2.5  # one 1 s poll, not 38.6 h
    assert status == 429
    assert int(headers["retry-after"]) >= _INCIDENT_RETRY_AFTER_S - 5  # honest window
    assert f"{int(headers['retry-after'])} s".encode() in body
    assert any(
        line.startswith("parked-terminal ")
        and "reason=no_sibling_account_closed_past_hold_bound" in line
        for line in lines
    )
    assert not any(line.startswith("parked-reroute ") for line in lines)
    assert pool.upstream.count.get("B", 0) == 0  # never sent anywhere
    await _drain(pool, busy, lim_a, lim_b)  # in-flight / queued back to zero


async def test_a_streaming_hold_ends_with_the_terminal_sse_error(pool) -> None:
    """The incident shape: a committed SSE hold (pings) must end with an error event."""
    lim_a, lim_b, busy, parked = await _park_no_sibling(
        pool, stream=True, headers={"X-Anthropic-Throttle-Client-Patience-Ms": "600000"}
    )
    status, _headers, body = await asyncio.wait_for(parked, 6)
    assert status == 200  # already committed; the status can no longer change
    assert b"event: error" in body and b"closed for" in body and b"will not help" in body
    await _drain(pool, busy, lim_a, lim_b)


async def test_priority_lane_is_exempt_from_the_terminal(pool) -> None:
    lim_a, lim_b, busy, parked = await _park_no_sibling(pool, max_tokens=16)
    await asyncio.sleep(2.5)  # two polls
    assert not parked.done() and lim_a.queued_total == 1
    parked.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await parked
    await _drain(pool, busy, lim_a, lim_b)


async def test_a_sibling_still_moves_it_instead_of_ending_it(pool, lines) -> None:
    lim_a, lim_b, busy, parked = await _park_no_sibling(pool)
    lim_b._retry_after_until = 0.0  # B reopens inside the poll
    status, headers, body = await asyncio.wait_for(parked, 6)
    assert status == 200 and json.loads(body)["seat"] == "B"
    assert not any(line.startswith("parked-terminal ") for line in lines)
    await _drain(pool, busy, lim_a, lim_b)


async def test_a_short_window_is_not_ended_even_with_no_sibling(pool) -> None:
    lim_a = await _open_seat(pool, "A", live=1)
    limiter_b = await _open_seat(pool, "B", live=1)
    limiter_b.note_retry_after(30)
    assert proxy._parked_terminal_for(lim_a, pool.bids["A"], "v1/messages", "m") is None
    lim_a.note_retry_after(config.MAX_HOLD_RETRY_AFTER_S / 2)
    assert proxy._parked_terminal_for(lim_a, pool.bids["A"], "v1/messages", "m") is None
    lim_a.note_retry_after(config.MAX_HOLD_RETRY_AFTER_S * 4)
    assert proxy._parked_terminal_for(lim_a, pool.bids["A"], "v1/messages", "m") is not None
