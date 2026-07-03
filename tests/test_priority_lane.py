"""Priority lane: short/latency-sensitive calls must not starve behind long
generations.

Regression for the 03/07/2026 /goal Stop-hook evaluator halt — a 24s sonnet
eval waited 46s in the single FIFO past its 30s client timeout because every
main slot was held by a 100-600s generation, so the client disconnected and
Claude Code reported the misleading "issue with the selected model (sonnet)".
The fix reserves a small band of slots ABOVE the live AIMD ceiling for calls
classified short (small max_tokens, no tools).
"""

import asyncio
import json

import pytest

from anthropic_throttle_proxy import config, limiter
from anthropic_throttle_proxy.ratelimit import _short_request_hint


async def _yield_loop(tries: int = 10) -> None:
    """Let pending acquire() tasks reach their ``await fut`` park point."""
    for _ in range(tries):
        await asyncio.sleep(0)


async def test_priority_jumps_saturated_main_pool(monkeypatch) -> None:
    """The core fix: a short call dispatches through the reserve while every
    main slot is held by a long generation and a normal call stays queued."""
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

    # A priority (evaluator) call dispatches immediately via the reserved band.
    await asyncio.wait_for(lim.acquire("evalcid", priority=True), timeout=1.0)
    assert lim.inflight == 3  # 2 main + 1 reserve
    assert not normal.done()  # reserve is priority-only; normal still waits

    normal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await normal


async def test_priority_reserve_is_bounded(monkeypatch) -> None:
    """Priority may exceed max_concurrent by at most PRIORITY_RESERVE_SLOTS —
    the overshoot of the AIMD ceiling is bounded, not unlimited."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 2)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("long1")  # main pool (1) full
    await asyncio.wait_for(lim.acquire("e1", priority=True), timeout=1.0)
    await asyncio.wait_for(lim.acquire("e2", priority=True), timeout=1.0)
    assert lim.inflight == 3  # 1 main + 2 reserve — ceiling reached

    third = asyncio.create_task(lim.acquire("e3", priority=True))
    await _yield_loop()
    assert not third.done()  # exceeds the reserve → queues
    assert lim.inflight == 3

    await lim.release()  # frees one reserve slot
    await _yield_loop()
    assert third.done()
    third.result()


async def test_priority_drains_before_round_robin(monkeypatch) -> None:
    """With no reserve headroom, a freed slot goes to the priority queue first."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("holder")  # main pool (1) full, inflight=1

    normal = asyncio.create_task(lim.acquire("ncid"))
    await _yield_loop()
    prio = asyncio.create_task(lim.acquire("pcid", priority=True))
    await _yield_loop()
    assert not normal.done() and not prio.done()

    await lim.release()  # exactly one slot frees; priority must win it
    await _yield_loop()
    assert prio.done()
    assert not normal.done()
    prio.result()

    await lim.release()  # drain the priority holder so the normal can finish
    await _yield_loop()
    assert normal.done()
    normal.result()


async def test_priority_cancel_cleans_priority_queue(monkeypatch) -> None:
    """A cancelled priority waiter is pruned from the priority queue (no leak)."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)
    lim = limiter.FairBearerLimiter(1, "fair")
    await lim.acquire("holder")  # full
    prio = asyncio.create_task(lim.acquire("pcid", priority=True))
    await _yield_loop()
    assert len(lim._priority_queue) == 1

    prio.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prio
    assert len(lim._priority_queue) == 0


def test_short_request_hint_classifies_evaluator_vs_generation() -> None:
    """The classifier: evaluator (small max_tokens, no tools) vs generation."""
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
