"""Queue-DEPTH admission: reject what the lane can prove it cannot drain.

The wait bound (PR #83) answers "how long may a request wait". It never asked
whether the wait was possible. Measured 01/09/2026 14:20-14:24 BRT on the
`:8766` Z.AI lane: ``max_concurrent=2``, queue depth 3, inherited wait budget
30 s, and the two occupied slots completed after 113.7 s and 221.2 s. The
arriving request needed two service rounds behind three queued peers — it could
not start inside 30 s by construction. The proxy parked it anyway, burned the
whole budget in silence, and advertised ``Retry-After: 5`` against a >= 442 s
drain, so the client retried the same wall four times and aborted.

Timings here are the incident replayed at 1/100 scale.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.limiter import (
    FairBearerLimiter,
    QueueWaitTimeout,
    _compute_drain,
)

SCALE = 100.0
INCIDENT_SLOTS = 2
INCIDENT_QUEUED = 3
INCIDENT_BUDGET_S = 30.0 / SCALE
INCIDENT_HOLDS_S = (113.7 / SCALE, 221.2 / SCALE)
INCIDENT_COLD_S = 10.0 / SCALE
# Long enough that the held slots are themselves evidence the lane is slow —
# the estimator refuses to reject on a guess.
INCIDENT_SETTLE_S = 0.2


async def _saturated_incident_limiter(monkeypatch) -> tuple[FairBearerLimiter, list[asyncio.Task]]:
    """The measured shape: 2 slots held by long generations, 3 parked behind."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", INCIDENT_COLD_S)
    lim = FairBearerLimiter(INCIDENT_SLOTS, "fair")
    lim.max_concurrent = INCIDENT_SLOTS
    tasks: list[asyncio.Task] = []
    for hold_s in INCIDENT_HOLDS_S:
        await lim.acquire("holder")

        async def _hold(d: float = hold_s) -> None:
            await asyncio.sleep(d)
            await lim.release()

        tasks.append(asyncio.create_task(_hold()))
    tasks += [asyncio.create_task(lim.acquire(f"queued{i}")) for i in range(INCIDENT_QUEUED)]
    for _ in range(100):
        if lim.snapshot()["queued_total"] == INCIDENT_QUEUED:
            break
        await asyncio.sleep(0.001)
    assert lim.snapshot()["queued_total"] == INCIDENT_QUEUED
    assert lim.snapshot()["inflight"] == INCIDENT_SLOTS
    await asyncio.sleep(INCIDENT_SETTLE_S)
    return lim, tasks


async def _drain(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_incident_arithmetic_at_the_measured_scale() -> None:
    """The measured numbers, unscaled: 2 slots, 3 queued, 30 s, ~113.7 s each."""
    estimate = _compute_drain(
        slots=2,
        busy=2,
        queued=3,
        service_time_s=113.7,
        samples=8,
        source="measured",
        evidenced=True,
        max_wait=30.0,
    )
    assert estimate.rounds == 2, "4th in line behind 3 queued on 2 servers"
    assert estimate.wait_s == pytest.approx(227.4)
    assert estimate.admits is False
    assert estimate.rejects is True
    assert estimate.max_depth == 0
    # Honest: 228 s, not 5 s. Bounded by the configured ceiling.
    assert estimate.retry_after_s == 228
    assert estimate.retry_after_s <= config.QUEUE_RETRY_AFTER_MAX_S


async def test_incident_shape_rejects_before_enqueue(monkeypatch) -> None:
    """The load-bearing case: reject pre-queue, leave the queue untouched."""
    lim, tasks = await _saturated_incident_limiter(monkeypatch)
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(QueueWaitTimeout) as caught:
            async with lim.slot("arriving", max_wait=INCIDENT_BUDGET_S):
                raise AssertionError("an undrainable request must never hold a slot")
        elapsed = loop.time() - started
        assert caught.value.pre_queue is True
        # Rejected on arithmetic, not by burning the client's patience.
        assert elapsed < INCIDENT_BUDGET_S / 2
        snap = lim.snapshot()
        assert snap["queued_total"] == INCIDENT_QUEUED
        assert snap["inflight"] == INCIDENT_SLOTS
        assert snap["max_concurrent"] == INCIDENT_SLOTS
    finally:
        await _drain(tasks)


async def test_rejection_retry_after_is_bounded_and_not_shorter_than_the_budget(
    monkeypatch,
) -> None:
    """Advertising 5 s against a 442 s drain is what produced the retry storm."""
    lim, tasks = await _saturated_incident_limiter(monkeypatch)
    try:
        with pytest.raises(QueueWaitTimeout) as caught:
            async with lim.slot("arriving", max_wait=INCIDENT_BUDGET_S):
                pass
        resp = proxy._queue_wait_timeout_response(
            "bid00000", "arriving", "v1/messages", lim, INCIDENT_BUDGET_S, timeout=caught.value
        )
        assert resp.status == 503
        assert resp.headers[config.QUEUE_TIMEOUT_HEADER] == "1"
        retry_raw = resp.headers["retry-after"]
        assert retry_raw == str(int(retry_raw)), "Retry-After must be an integer number of seconds"
        retry = int(retry_raw)
        # Never shorter than the historical floor, than the budget just proven
        # insufficient, or than the next plausible slot release.
        estimate = caught.value.estimate
        assert retry >= config.QUEUE_TIMEOUT_RETRY_AFTER_S
        assert retry >= math.ceil(INCIDENT_BUDGET_S)
        assert retry >= math.ceil(estimate.service_time_s)
        assert retry <= config.QUEUE_RETRY_AFTER_MAX_S
    finally:
        await _drain(tasks)


async def test_elapsed_timeout_also_reports_a_drain_estimate(monkeypatch) -> None:
    """The already-parked path (budget expiry) gets the same honest interval."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 60.0)
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    await lim.acquire("holder")
    resp = proxy._queue_wait_timeout_response("bid00000", "cid", "v1/messages", lim, 30.0)
    assert int(resp.headers["retry-after"]) >= 30
    await lim.release()


async def test_guess_only_lanes_never_reject(monkeypatch) -> None:
    """No completion and no long hold = a guess, and a guess must not refuse."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 3600.0)
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    await lim.acquire("holder")
    estimate = lim.drain_estimate(0.05)
    assert estimate.admits is False, "the arithmetic says it cannot drain"
    assert estimate.evidenced is False
    assert estimate.rejects is False, "but the lane has measured nothing yet"
    waiter = asyncio.create_task(_park(lim, "arriving", 5.0))
    await asyncio.sleep(0.01)
    assert lim.snapshot()["queued_total"] == 1
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    await lim.release()


async def test_free_slot_is_always_admitted(monkeypatch) -> None:
    """A cold lane with capacity must behave exactly as before."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 3600.0)
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    async with lim.slot("c1", max_wait=0.05):
        assert lim.snapshot()["inflight"] == 1
        # Second slot is free: no queueing, so no drain question to ask.
        async with lim.slot("c2", max_wait=0.05):
            assert lim.snapshot()["inflight"] == 2
    assert lim.snapshot()["inflight"] == 0


async def test_cold_history_still_admits_a_drainable_queue(monkeypatch) -> None:
    """No completions yet: the cold estimate must not reject a feasible wait."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 0.02)
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    for _ in range(2):
        await lim.acquire("holder")
    parked = [asyncio.create_task(lim.acquire(f"q{i}")) for i in range(3)]
    await asyncio.sleep(0.005)

    estimate = lim.drain_estimate(5.0)
    assert estimate.source == "cold"
    assert estimate.samples == 0
    assert estimate.admits is True

    waiter = asyncio.create_task(_park(lim, "arriving", 5.0))
    await asyncio.sleep(0.01)
    assert not waiter.done(), "a drainable queue must still park, not reject"

    waiter.cancel()
    for task in parked:
        task.cancel()
    await asyncio.gather(waiter, *parked, return_exceptions=True)
    for _ in range(4):
        await lim.release()


async def _park(lim: FairBearerLimiter, cid: str, max_wait: float) -> bool:
    async with lim.slot(cid, max_wait=max_wait):
        return True


async def test_unbounded_and_zero_bounds_keep_their_meaning(monkeypatch) -> None:
    """max_wait None = historical unbounded park; 0/False = untouched fast path."""
    lim, tasks = await _saturated_incident_limiter(monkeypatch)
    try:
        waiter = asyncio.create_task(_park(lim, "unbounded", None))
        await asyncio.sleep(0.01)
        assert not waiter.done()
        assert lim.snapshot()["queued_total"] == INCIDENT_QUEUED + 1
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    finally:
        await _drain(tasks)


async def test_non_queue_modes_never_reject(monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 3600.0)
    for mode in ("off", "observe"):
        lim = FairBearerLimiter(1, mode)
        lim.max_concurrent = 1
        async with lim.slot("c1", max_wait=0.001):
            async with lim.slot("c2", max_wait=0.001):
                assert lim.inflight == 2


async def test_priority_reserve_is_judged_against_its_own_pool(monkeypatch) -> None:
    """The lane has a dedicated pool; a full main pool must not reject it."""
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
    lim, tasks = await _saturated_incident_limiter(monkeypatch)
    try:
        async with lim.slot("evaluator", priority=True, max_wait=INCIDENT_BUDGET_S) as held:
            assert held.priority is True
            assert lim.priority_inflight == 1
    finally:
        await _drain(tasks)


async def test_priority_lane_saturation_rejects_too(monkeypatch) -> None:
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 1)
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 0.01)
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    await lim.acquire("holder", priority=True)
    await asyncio.sleep(0.08)  # the reserve slot is measurably slow
    with pytest.raises(QueueWaitTimeout) as caught:
        async with lim.slot("evaluator", priority=True, max_wait=0.05):
            raise AssertionError("reserve pool is full and cannot drain in time")
    assert caught.value.pre_queue is True
    assert caught.value.estimate.slots == 1
    await lim.release(priority=True)


async def test_completed_slots_teach_the_estimator() -> None:
    """Three completions of ~50 ms move the source from cold to measured."""
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    for _ in range(3):
        async with lim.slot("c1", max_wait=5.0):
            await asyncio.sleep(0.05)
    estimate = lim.drain_estimate(5.0)
    assert estimate.samples == 3
    assert estimate.source == "measured"
    assert 0.04 <= estimate.service_time_s <= 0.5


async def test_held_slot_elapsed_beats_a_stale_fast_history(monkeypatch) -> None:
    """A slot held longer than any completed sample IS evidence of a slow lane."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 0.001)
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    for _ in range(3):
        async with lim.slot("c1", max_wait=5.0):
            await asyncio.sleep(0.005)
    await lim.acquire("slow-holder")
    await asyncio.sleep(0.08)
    estimate = lim.drain_estimate(5.0)
    assert estimate.source == "inflight"
    assert estimate.service_time_s >= 0.07
    await lim.release()


async def test_cancel_while_parked_leaks_nothing() -> None:
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    await lim.acquire("holder")
    # Budget generous enough for the cold estimate, so the request really parks.
    waiter = asyncio.create_task(_park(lim, "cancelled", config.QUEUE_DRAIN_DEFAULT_S * 6))
    await asyncio.sleep(0.01)
    assert lim.snapshot()["queued_total"] == 1
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    await asyncio.sleep(0.02)
    await lim.release()
    snap = lim.snapshot()
    assert snap["inflight"] == 0
    assert snap["queued_total"] == 0
    # Only the holder completed; the parked-then-cancelled request was never
    # dispatched, so it is neither a sample nor an orphaned dispatch stamp.
    estimate = lim.drain_estimate(5.0)
    assert estimate.samples == 1
    assert estimate.service_time_s >= 0.02


async def test_cancelled_dispatch_records_no_sample_and_leaks_nothing() -> None:
    """A slot cancelled during the dispatch race never ran: it is not a sample."""
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    await lim.acquire("holder")
    waiter = asyncio.create_task(_park(lim, "racing", config.QUEUE_DRAIN_DEFAULT_S * 6))
    await asyncio.sleep(0.03)
    assert lim.snapshot()["queued_total"] == 1
    # release() dispatches the parked future synchronously; cancelling before
    # the waiter task resumes is exactly the dispatch race _cancel_cleanup owns.
    await lim.release()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    snap = lim.snapshot()
    assert snap["inflight"] == 0
    assert snap["queued_total"] == 0
    estimate = lim.drain_estimate(5.0)
    assert estimate.samples == 1, "the racing request never ran upstream"
    assert estimate.service_time_s >= 0.03, "and its ~0s must not become the estimate"


def test_admission_publishes_a_positive_depth_bound() -> None:
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    block = proxy._lane_saturation({"b": {"limiter": lim.snapshot()}}, {"b": True})
    assert isinstance(block["queue_admit_max_depth"], int)
    assert block["queue_admit_max_depth"] > 0
    inputs = block["queue_admit"]
    assert inputs["source"] == "cold"
    assert inputs["samples"] == 0
    assert inputs["slots"] == 2
    assert inputs["max_wait_s"] == float(config.QUEUE_MAX_WAIT_S)


def test_admission_depth_bound_survives_a_lane_with_no_bearers() -> None:
    """Cold process: publish the config-derived bound, never a zero/None."""
    block = proxy._lane_saturation({}, {})
    assert block["queue_admit_max_depth"] > 0
    assert block["queue_admit"]["source"] == "config"


def test_admission_depth_bound_shrinks_as_the_lane_slows(monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)
    fast = FairBearerLimiter(2, "fair")
    fast.max_concurrent = 2
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 1.0)
    shallow_fast = proxy._lane_saturation({"b": {"limiter": fast.snapshot()}}, {"b": True})
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 120.0)
    shallow_slow = proxy._lane_saturation({"b": {"limiter": fast.snapshot()}}, {"b": True})
    assert shallow_fast["queue_admit_max_depth"] > shallow_slow["queue_admit_max_depth"]
