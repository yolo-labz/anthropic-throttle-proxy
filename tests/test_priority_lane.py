"""Priority lane: short/latency-sensitive calls must not starve behind long
generations.

Regression for the 03/07/2026 /goal Stop-hook evaluator halt — a 24s sonnet
eval waited 46s in the single FIFO past its 30s client timeout because every
main slot was held by a 100-600s generation, so the client disconnected and
Claude Code reported the misleading "issue with the selected model (sonnet)".
The fix dispatches calls classified short (small max_tokens, no tools, small
body) from a DEDICATED pool of PRIORITY_RESERVE_SLOTS, independent of the
main AIMD pool — total upstream concurrency ≤ max_concurrent + reserve.
"""

import asyncio
import json

import pytest

from anthropic_throttle_proxy import config, limiter
from anthropic_throttle_proxy.proxy import _is_priority_request
from anthropic_throttle_proxy.ratelimit import _short_request_hint


async def _yield_loop(tries: int = 10) -> None:
    """Let pending acquire() tasks reach their ``await fut`` park point."""
    for _ in range(tries):
        await asyncio.sleep(0)


async def test_priority_jumps_saturated_main_pool(monkeypatch) -> None:
    """The core fix: a short call dispatches through the dedicated pool while
    every main slot is held by a long generation and a normal call stays queued."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 2)
    lim = limiter.FairBearerLimiter(2, "fair")
    # Live cap starts at the AIMD initial (1), not hard_max — warm it to 2 so
    # two long holders actually saturate the main pool.
    lim.max_concurrent = 2
    await lim.acquire("long1")
    await lim.acquire("long2")
    assert lim.inflight == 2  # main pool (max_concurrent=2) saturated by long holders

    # A normal call cannot dispatch — no free main slot (this is the starvation).
    normal = asyncio.create_task(lim.acquire("normalcid"))
    await _yield_loop()
    assert not normal.done()

    # A priority (evaluator) call dispatches immediately via the dedicated pool.
    assert await asyncio.wait_for(lim.acquire("evalcid", priority=True), timeout=1.0)
    assert lim.inflight == 3  # 2 main + 1 priority
    assert lim.priority_inflight == 1
    assert not normal.done()  # the lane is priority-only; normal still waits

    normal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await normal


async def test_priority_survives_shrink_with_stale_inflight(monkeypatch) -> None:
    """Codex BLOCKER regression: after an AIMD shrink, stale main inflight sits
    ABOVE the new ceiling. A shared-ceiling reserve (`inflight < cap + reserve`)
    would be pinched shut in exactly the motivating 429 storm; the dedicated
    pool must keep dispatching evaluators."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 2)
    lim = limiter.FairBearerLimiter(4, "fair")
    lim.max_concurrent = 4
    for i in range(3):
        await lim.acquire(f"long{i}")
    lim.max_concurrent = 1  # AIMD shrink to floor; 3 long holders are stale
    assert lim.inflight == 3

    # Priority must still dispatch: its pool is independent of the main ceiling.
    assert await asyncio.wait_for(lim.acquire("evalcid", priority=True), timeout=1.0)
    assert lim.inflight == 4
    assert lim.priority_inflight == 1

    # And a normal call must NOT dispatch (main pool over its shrunk ceiling).
    normal = asyncio.create_task(lim.acquire("normalcid"))
    await _yield_loop()
    assert not normal.done()
    normal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await normal


async def test_priority_reserve_is_bounded_and_lane_independent(monkeypatch) -> None:
    """Priority concurrency is capped at PRIORITY_RESERVE_SLOTS, and the pools
    do not leak into each other: releasing a MAIN slot must not dispatch a
    queued PRIORITY waiter (and the lane frees only on a priority release)."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 2)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("long1")  # main pool (cap 1) full
    await asyncio.wait_for(lim.acquire("e1", priority=True), timeout=1.0)
    await asyncio.wait_for(lim.acquire("e2", priority=True), timeout=1.0)
    assert lim.inflight == 3  # 1 main + 2 priority — both pools full
    assert lim.priority_inflight == 2

    third = asyncio.create_task(lim.acquire("e3", priority=True))
    await _yield_loop()
    assert not third.done()  # lane full → queues in the lane

    await lim.release()  # frees the MAIN slot — must NOT feed the lane
    await _yield_loop()
    assert not third.done()
    assert lim.priority_inflight == 2

    await lim.release(priority=True)  # frees a LANE slot
    await _yield_loop()
    assert third.done()
    third.result()
    assert lim.priority_inflight == 2  # e3 took the freed lane slot


async def test_sustained_priority_load_does_not_starve_normal(monkeypatch) -> None:
    """Codex MAJOR: the lane must not eat main-pool slots. With the lane
    saturated and more priority calls queued, a normal call still dispatches
    into free main capacity."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
    lim = limiter.FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    await lim.acquire("long1")  # main: 1 of 2 used
    await lim.acquire("e1", priority=True)  # lane: 1 of 1 used
    p2 = asyncio.create_task(lim.acquire("e2", priority=True))
    await _yield_loop()
    assert not p2.done()  # lane full → queued

    # Normal traffic must still see the free main slot (main usage counts
    # inflight MINUS priority_inflight against max_concurrent).
    await asyncio.wait_for(lim.acquire("normalcid"), timeout=1.0)
    assert lim.inflight == 3  # 2 main + 1 priority
    assert not p2.done()  # queued priority never stole main capacity

    p2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await p2


async def test_priority_lane_round_robin_per_client(monkeypatch) -> None:
    """Codex MAJOR: the fairness invariant holds INSIDE the lane — one chatty
    client's evaluators cannot starve a sibling's. Arrival a1, a2, b1 must
    dispatch a1, b1 (RR rotates), then a2."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("e0", priority=True)  # lane (1 slot) full

    a1 = asyncio.create_task(lim.acquire("clientA", priority=True))
    await _yield_loop()
    a2 = asyncio.create_task(lim.acquire("clientA", priority=True))
    await _yield_loop()
    b1 = asyncio.create_task(lim.acquire("clientB", priority=True))
    await _yield_loop()
    assert not a1.done() and not a2.done() and not b1.done()

    await lim.release(priority=True)
    await _yield_loop()
    assert a1.done() and not a2.done() and not b1.done()  # A first (arrival)

    await lim.release(priority=True)
    await _yield_loop()
    assert b1.done() and not a2.done()  # RR rotates to B before A's second

    await lim.release(priority=True)
    await _yield_loop()
    assert a2.done()
    a1.result(), a2.result(), b1.result()


async def test_reserve_zero_demotes_priority_to_normal(monkeypatch) -> None:
    """Reserve 0 disables the lane: a priority call is demoted to normal
    round-robin traffic at acquire time (returns False), keeping accounting
    symmetric and the total bound at max_concurrent exactly."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("holder")  # main pool (1) full

    prio = asyncio.create_task(lim.acquire("pcid", priority=True))
    await _yield_loop()
    assert not prio.done()  # no lane → waits like normal traffic
    assert lim.snapshot()["priority_queued"] == 0  # parked in the NORMAL queue

    await lim.release()
    await _yield_loop()
    assert prio.done()
    assert prio.result() is False  # demoted — caller must release(priority=False)
    assert lim.priority_inflight == 0
    await lim.release()


async def test_reserve_lowered_to_zero_migrates_parked_lane_waiters(monkeypatch) -> None:
    """Codex round-2 MAJOR: reserve hot-tuned to 0 with lane waiters already
    parked must not strand them — the next dispatch event migrates them into
    the normal queue, and they complete DEMOTED (result False) so release
    accounting matches the pool that actually granted the slot."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("p1", priority=True)  # lane (1) full
    queued = asyncio.create_task(lim.acquire("p2", priority=True))
    await _yield_loop()
    assert lim.snapshot()["priority_queued"] == 1

    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)  # lane disabled
    await lim.release(priority=True)  # frees main capacity + triggers dispatch
    await _yield_loop()
    assert queued.done()
    assert queued.result() is False  # dispatched via the NORMAL pool
    assert lim.snapshot()["priority_queued"] == 0
    assert lim.priority_inflight == 0
    await lim.release()  # demoted slot releases as normal — no counter drift
    assert lim.inflight == 0


async def test_reserve_raised_kick_wakes_parked_lane_waiters(monkeypatch) -> None:
    """Codex round-2 MAJOR (raise direction): raising the reserve must wake
    already-parked lane waiters via kick_existing_limiters — without waiting
    for unrelated traffic to trigger a dispatch event."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
    lim = limiter.FairBearerLimiter(1, "fair")
    monkeypatch.setitem(config.bearer_limiters, "testbearer", lim)
    await lim.acquire("p1", priority=True)  # lane (1) full
    queued = asyncio.create_task(lim.acquire("p2", priority=True))
    await _yield_loop()
    assert not queued.done()

    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 2)
    await limiter.kick_existing_limiters()  # what _set_priority_reserve_slots schedules
    await _yield_loop()
    assert queued.done()
    assert queued.result() is True  # dispatched via the (now larger) lane
    assert lim.priority_inflight == 2


async def test_priority_cancel_cleans_priority_queue(monkeypatch) -> None:
    """A cancelled priority waiter is pruned from the lane queue (no leak)."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("e0", priority=True)  # lane full
    prio = asyncio.create_task(lim.acquire("pcid", priority=True))
    await _yield_loop()
    assert lim.snapshot()["priority_queued"] == 1

    prio.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prio
    assert lim.snapshot()["priority_queued"] == 0
    assert "pcid" not in lim._priority_queues  # per-client deque fully removed


def test_short_request_hint_classifies_evaluator_vs_generation() -> None:
    """The parser: evaluator (small max_tokens, no tools) vs generation."""
    # Evaluator shape → (max_tokens, has_tools=False) → priority-eligible.
    assert _short_request_hint(json.dumps({"max_tokens": 1024}).encode()) == (1024, False)
    # Generation shape carries tools → excluded from the priority lane.
    mt, has_tools = _short_request_hint(
        json.dumps({"max_tokens": 32000, "tools": [{"name": "Read"}]}).encode()
    )
    assert mt == 32000
    assert has_tools is True
    # Fail-safe: unparseable / missing max_tokens → (None, False) → never priority.
    assert _short_request_hint(b"not json") == (None, False)
    assert _short_request_hint(None) == (None, False)
    # JSON `true` decodes to bool (an int subclass) — must NOT count as max_tokens.
    assert _short_request_hint(json.dumps({"max_tokens": True}).encode()) == (None, False)


def test_is_priority_request_gates(monkeypatch) -> None:
    """Codex MAJOR: max_tokens caps only the OUTPUT — a giant no-tools prompt
    (e.g. 6MB body, max_tokens 8192) must not jump the queue. Every gate fails
    safe toward the normal lane."""
    monkeypatch.setattr(config, "PRIORITY_MAX_TOKENS", 8192)
    monkeypatch.setattr(config, "PRIORITY_MAX_BODY_BYTES", 262144)
    assert _is_priority_request(1024, False, 2048) is True
    assert _is_priority_request(8192, False, 262144) is True  # boundary inclusive
    # Body too large — the false-positive route Codex flagged.
    assert _is_priority_request(8192, False, 6 * 1024 * 1024) is False
    # Tools present, max_tokens absent/zero/too big.
    assert _is_priority_request(1024, True, 2048) is False
    assert _is_priority_request(None, False, 2048) is False
    assert _is_priority_request(0, False, 2048) is False
    assert _is_priority_request(8193, False, 2048) is False
