"""Offline one-slot fairness despite measured service exceeding the wait budget."""

from __future__ import annotations

import asyncio

import pytest

from anthropic_throttle_proxy import config
from anthropic_throttle_proxy.limiter import FairBearerLimiter, QueueWaitTimeout


async def _slow_lane(monkeypatch, enabled: bool = True) -> FairBearerLimiter:
    monkeypatch.setattr(config, "QUEUE_ALLOW_FIRST_WAITER", enabled, raising=False)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 1)
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)
    lim = FairBearerLimiter(1, "fair")
    await lim.acquire("ipad-a")
    lim._samples.extend([236.319] * 3)
    return lim


async def _parked(lim: FairBearerLimiter, task: asyncio.Task) -> None:
    for _ in range(100):
        if lim.queued_total == 1:
            return
        if task.done():
            await task
        await asyncio.sleep(0)
    raise AssertionError("first waiter must park")


async def _wait(lim: FairBearerLimiter, client: str, budget: float = 1.0) -> None:
    async with lim.slot(client, max_wait=budget):
        assert lim.inflight == 1, "queue admission never creates another upstream slot"


async def test_first_waiter_gets_next_slot(monkeypatch) -> None:
    lim = await _slow_lane(monkeypatch)
    started = next(iter(lim._holds.values()))[1]
    estimate = lim.drain_estimate(180.0, now=started + 1.779)
    assert estimate.rejects and estimate.queued == 0 and estimate.wait_s > 180.0
    assert estimate.residual_s == pytest.approx(234.54)
    waiter = asyncio.create_task(_wait(lim, "ipad-b", 180.0))
    try:
        await _parked(lim, waiter)
        with pytest.raises(QueueWaitTimeout) as refused:
            async with lim.slot("ipad-a", max_wait=180.0):
                raise AssertionError("holder cannot jump its parked sibling")
        assert refused.value.pre_queue
        assert lim.inflight == 1 and lim.queued_total == 1
        await lim.release()
        await asyncio.wait_for(waiter, 1.0)
        assert lim.inflight == 0 and lim.queued_total == 0
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_simultaneous_arrivals_reserve_only_one_first_waiter(monkeypatch) -> None:
    lim = await _slow_lane(monkeypatch)
    tasks = [asyncio.create_task(_wait(lim, f"ipad-{i}")) for i in range(4)]
    try:
        await _parked(lim, tasks[0])
        for task in tasks[1:]:
            with pytest.raises(QueueWaitTimeout) as refused:
                await task
            assert refused.value.pre_queue
        assert lim.queued_total == 1 and lim.inflight == 1
        await lim.release()
        await asyncio.wait_for(tasks[0], 1.0)
        assert lim.queued_total == 0 and lim.inflight == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_first_waiter_actual_timeout_cleans_queue(monkeypatch) -> None:
    lim = await _slow_lane(monkeypatch)
    with pytest.raises(QueueWaitTimeout) as expired:
        async with lim.slot("ipad-b", max_wait=0.03):
            raise AssertionError("occupied slot must not dispatch")
    assert not expired.value.pre_queue, "the original finite timeout must actually expire"
    assert lim.queued_total == 0 and lim.inflight == 1
    assert len(lim._holds) == 1 and len(lim._samples) == 3
    await lim.release()


async def test_cancelled_first_waiter_reopens_one_place(monkeypatch) -> None:
    lim = await _slow_lane(monkeypatch)
    waiter = asyncio.create_task(_wait(lim, "ipad-b"))
    try:
        await _parked(lim, waiter)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert lim.queued_total == 0 and lim.inflight == 1
        assert len(lim._holds) == 1 and len(lim._samples) == 3
        replacement = asyncio.create_task(_wait(lim, "ipad-c"))
        try:
            await _parked(lim, replacement)
            await lim.release()
            await asyncio.wait_for(replacement, 1.0)
            assert lim.inflight == 0 and lim.queued_total == 0
        finally:
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("case", ["default-off", "two-slots", "priority"])
async def test_other_admission_paths_keep_predictive_refusal(monkeypatch, case: str) -> None:
    lim = await _slow_lane(monkeypatch, enabled=case != "default-off")
    if case == "two-slots":
        lim.max_concurrent = 2
        await lim.acquire("other-holder")
    if case == "priority":
        monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
        await lim.acquire("priority-holder", priority=True)
        lim._priority_samples.extend([236.319] * 3)
    with pytest.raises(QueueWaitTimeout) as refused:
        async with lim.slot("ipad-b", priority=case == "priority", max_wait=180.0):
            raise AssertionError("the exception must remain confined to one normal slot")
    assert refused.value.pre_queue and lim.queued_total == 0 and lim.priority_queued == 0
    while lim.inflight:
        await lim.release(priority=lim.priority_inflight > 0)
