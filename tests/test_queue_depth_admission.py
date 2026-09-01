"""Queue-DEPTH admission: reject what the lane can prove it cannot drain.

The wait bound (PR #83) answers "how long may a request wait". It never asked
whether the wait was possible. Measured 01/09/2026 14:20-14:24 BRT on the
`:8766` Z.AI lane: ``max_concurrent=2``, queue depth 3, inherited wait budget
30 s, and the two occupied slots completed after 113.7 s and 221.2 s. The
proxy parked the arrival without consulting any of that, burned the whole
budget in silence, and advertised ``Retry-After: 5``, so the client retried the
same wall four times and aborted.

The record fixes neither the arrival's per-client rotation rank nor the
holders' ages at that instant, so the tests below that use the measured numbers
are bounds under stated assumptions, not a replay of the event (see
``specs/221-zai-queue-depth-admission/evidence.md`` §5).

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
    budget_floored_retry_after,
)

SCALE = 100.0
INCIDENT_SLOTS = 2
INCIDENT_QUEUED = 3
INCIDENT_BUDGET_S = 30.0 / SCALE
INCIDENT_HOLDS_S = (113.7 / SCALE, 221.2 / SCALE)
INCIDENT_COLD_S = 10.0 / SCALE
# Long enough that the held slots are themselves evidence the lane is slow —
# the estimator refuses to reject on a guess.
INCIDENT_SETTLE_S = 0.3


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
        ahead=3,
        service_time_s=113.7,
        residuals=[113.7, 113.7],
        samples=8,
        source="measured",
        evidenced=True,
        max_wait=30.0,
    )
    assert estimate.rounds == 2, "4th in line on 2 servers, ASSUMING ahead == depth"
    assert estimate.wait_s == pytest.approx(227.4)
    assert estimate.admits is False
    assert estimate.rejects is True
    assert estimate.max_depth == 0
    # Honest: 228 s, not 5 s. Bounded by the configured ceiling.
    assert estimate.retry_after_s == 228
    assert estimate.retry_after_s <= config.QUEUE_RETRY_AFTER_MAX_S


def test_rejects_at_the_lanes_own_measured_service_rate() -> None:
    """Completed-history alone is enough: 29/08 measured ~35 s/req on 2 slots."""
    estimate = _compute_drain(
        slots=2,
        busy=2,
        queued=3,
        ahead=3,
        service_time_s=35.0,
        residuals=[35.0, 35.0],
        samples=16,
        source="measured",
        evidenced=True,
        max_wait=30.0,
    )
    assert estimate.wait_s == pytest.approx(70.0)
    assert estimate.rejects is True
    assert estimate.retry_after_s == 70


def test_nearly_finished_slots_are_not_charged_a_whole_round() -> None:
    """An arrival behind slots that are about to free must not be refused."""
    estimate = _compute_drain(
        slots=2,
        busy=2,
        queued=0,
        ahead=0,
        service_time_s=40.0,
        residuals=[2.0, 2.0],  # both holders are 38 s into a 40 s typical request
        samples=16,
        source="measured",
        evidenced=True,
        max_wait=30.0,
    )
    assert estimate.rounds == 1
    assert estimate.wait_s == pytest.approx(2.0)
    assert estimate.admits is True
    assert estimate.rejects is False


async def test_round_robin_position_not_raw_queue_depth(monkeypatch) -> None:
    """A new client overtakes a chatty client's backlog — that is the fair queue.

    Counting raw depth would refuse a fresh client stuck behind one chatty
    client's 20 parked requests, which the round-robin dispatcher serves on the
    very next release. The same arrival from the CHATTY client is correctly
    refused: it really is 20 of its own turns away.
    """
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 0.05)
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    for _ in range(2):
        await lim.acquire("holder")
    await asyncio.sleep(0.2)  # slots are overdue: evidenced, ~0.2 s service
    backlog = [asyncio.create_task(lim.acquire("chatty")) for _ in range(20)]
    await asyncio.sleep(0.01)
    assert lim.snapshot()["queued_total"] == 20

    fresh = lim.drain_estimate(1.0, client_id="fresh")
    assert fresh.ahead == 1, "one turn for the chatty client, then this one"
    assert fresh.rejects is False

    same = lim.drain_estimate(1.0, client_id="chatty")
    assert same.ahead == 20
    assert same.rejects is True

    waiter = asyncio.create_task(_park(lim, "fresh", 1.0))
    await asyncio.sleep(0.01)
    assert not waiter.done(), "the fresh client must be parked, not refused"
    assert lim.snapshot()["queued_total"] == 21

    waiter.cancel()
    for task in backlog:
        task.cancel()
    await asyncio.gather(waiter, *backlog, return_exceptions=True)
    for _ in range(2):
        await lim.release()


async def test_simultaneous_burst_is_serialized_not_all_admitted(monkeypatch) -> None:
    """Ten arrivals in one burst must not all pass one stale estimate.

    The check and the enqueue are one event-loop step because every `_lock`
    critical section in the limiter is synchronous, so the lock take never
    yields. This test pins that property: if a future refactor awaits inside a
    critical section, the burst starts admitting past the bound.
    """
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 0.05)
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    await lim.acquire("holder")
    await asyncio.sleep(0.12)
    bound = lim.drain_estimate(0.30, client_id="c0")
    assert bound.evidenced is True

    rejected: list[int] = []

    async def arrival(i: int) -> None:
        try:
            async with lim.slot(f"c{i}", max_wait=0.30):
                pass
        except QueueWaitTimeout:
            rejected.append(i)

    tasks = [asyncio.create_task(arrival(i)) for i in range(10)]
    await asyncio.sleep(0.02)
    queued = lim.snapshot()["queued_total"]
    assert queued <= bound.max_depth + 1, f"burst admitted {queued} past bound {bound.max_depth}"
    assert len(rejected) == 10 - queued

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await lim.release()


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


def test_non_finite_budget_never_reaches_the_arithmetic(monkeypatch) -> None:
    """An inherited budget is a client-supplied header; `1e400` parses to inf.

    Unguarded, `horizon // service` raises OverflowError on a request path and
    a non-finite field reaches `/__throttle/health` as invalid JSON.
    """
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 0.0)
    inherited = proxy._effective_queue_max_wait({config.WAIT_BUDGET_HEADER: "1e400"})
    assert inherited == math.inf
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    for budget in (inherited, float("nan"), -5.0):
        estimate = lim.drain_estimate(budget)
        assert estimate.enforced is False
        assert estimate.rejects is False
        for value in estimate.as_dict().values():
            assert not isinstance(value, float) or math.isfinite(value)


def test_zero_service_time_is_floored(monkeypatch) -> None:
    """A zero estimate would admit any depth while publishing a zero bound."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 0.0)
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    estimate = lim.drain_estimate(30.0)
    assert estimate.service_time_s > 0
    assert estimate.max_depth > 0


def test_retry_after_ceiling_is_a_hard_cap(monkeypatch) -> None:
    """A 600 s budget under a 300 s ceiling must advertise 300, not 600."""
    monkeypatch.setattr(config, "QUEUE_RETRY_AFTER_MAX_S", 300)
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    estimate = lim.drain_estimate(600.0)
    assert budget_floored_retry_after(estimate) == 300
    assert estimate.retry_after_s <= 300


def test_staggered_holders_are_priced_per_slot() -> None:
    """Two slots free at very different times; one scalar gets both cases wrong.

    Codex round-2 BLOCKER, both directions:

    * a nearly-done slot must not wave through an arrival whose real wait is
      the OTHER, stuck slot;
    * a stuck slot must not refuse an arrival that the other slot serves in a
      second.
    """
    stuck_then_predecessor = _compute_drain(
        slots=2,
        busy=2,
        queued=1,
        ahead=1,  # one queued predecessor takes the first freed slot
        service_time_s=200.0,
        residuals=[1.0, 200.0],  # aged 34 s of a 35 s typical, and aged 200 s
        samples=16,
        source="inflight",
        evidenced=True,
        max_wait=30.0,
    )
    assert stuck_then_predecessor.wait_s > 30.0
    assert stuck_then_predecessor.rejects is True

    one_slot_about_to_free = _compute_drain(
        slots=2,
        busy=2,
        queued=0,
        ahead=0,
        service_time_s=35.0,
        residuals=[1.0, 34.0],
        samples=16,
        source="measured",
        evidenced=True,
        max_wait=30.0,
    )
    assert one_slot_about_to_free.wait_s == pytest.approx(1.0)
    assert one_slot_about_to_free.rejects is False


def test_residual_cannot_exceed_a_full_service() -> None:
    """A slot cannot have more left than a whole modelled request.

    `_service_estimate` already guarantees it (an overdue holder is priced at
    exactly the service estimate); this pins the guard for a caller that
    passes inconsistent numbers.
    """
    estimate = _compute_drain(
        slots=2,
        busy=2,
        queued=0,
        ahead=0,
        service_time_s=1.0,
        residuals=[0.0, 100.0],
        samples=16,
        source="measured",
        evidenced=True,
        max_wait=30.0,
    )
    assert estimate.residual_s <= estimate.service_time_s
    assert estimate.wait_s == pytest.approx(0.0)


async def test_round_robin_counts_the_rotation_not_just_the_clients(monkeypatch) -> None:
    """A client at the head of the rotation is one turn ahead of its siblings."""
    monkeypatch.setattr(config, "QUEUE_DRAIN_DEFAULT_S", 0.05)
    lim = FairBearerLimiter(1, "fair")
    lim.max_concurrent = 1
    await lim.acquire("holder")
    a = asyncio.create_task(lim.acquire("A"))
    b = [asyncio.create_task(lim.acquire("B")) for _ in range(2)]
    await asyncio.sleep(0.01)
    assert list(lim.snapshot()["rr_order"]) == ["A", "B"]

    # A is at the head with one parked request: its next one waits for its own
    # request plus ONE of B's, not both.
    assert lim.drain_estimate(5.0, client_id="A").ahead == 2
    # B is behind A, so its next one waits for both of its own plus A's turn.
    assert lim.drain_estimate(5.0, client_id="B").ahead == 3
    # A newcomer joins the tail: one turn each for A and B.
    assert lim.drain_estimate(5.0, client_id="C").ahead == 2

    for task in (a, *b):
        task.cancel()
    await asyncio.gather(a, *b, return_exceptions=True)
    await lim.release()


def test_bypass_bearer_publishes_no_enforced_capacity() -> None:
    """An `off`-mode bearer never enforces admission — say so, don't invent it."""
    lim = FairBearerLimiter(2, "off")
    block = proxy._lane_saturation({"b": {"limiter": lim.snapshot()}}, {"b": True})
    inputs = block["queue_admit"]
    assert inputs["bypass_bearers"] == 1
    assert inputs["enforced"] is False
    assert inputs["source"] == "bypass"
    assert block["queue_admit_max_depth"] > 0  # advisory, still never a bare 0


async def test_unknown_lease_never_completes_another_live_slot() -> None:
    """A stale lease must not price a request that is still running."""
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    async with lim.slot("c1", max_wait=5.0):
        held_before = dict(lim._holds)
        async with lim._lock:
            lim._note_completion(999_999, False)
        assert lim._holds == held_before
        assert lim.drain_estimate(5.0).samples == 0


def test_oversubscribed_pool_waits_for_the_cap_not_the_first_completion() -> None:
    """After an AIMD shrink, inflight can exceed the live cap.

    `_try_dispatch` stays shut until normal occupancy falls back UNDER the cap,
    so the earliest completions only pay down the overshoot — they dispatch
    nobody. Treating every holder as a server let an arrival through on a
    completion the dispatcher ignores (Codex round-3 BLOCKER).
    """
    estimate = _compute_drain(
        slots=1,  # live cap shrank to 1
        busy=2,  # two requests still in flight from before the shrink
        queued=0,
        ahead=0,
        service_time_s=100.0,
        residuals=[1.0, 100.0],
        samples=16,
        source="measured",
        evidenced=True,
        max_wait=30.0,
    )
    assert estimate.wait_s == pytest.approx(100.0)
    assert estimate.rejects is True


async def test_shrunk_limiter_does_not_dispatch_on_the_first_release() -> None:
    """The same shape against the real limiter, so the model matches dispatch."""
    lim = FairBearerLimiter(2, "fair")
    lim.max_concurrent = 2
    first = await lim.acquire_lease("h1")
    second = await lim.acquire_lease("h2")
    lim.max_concurrent = 1  # AIMD shrink under two in-flight requests
    parked = asyncio.create_task(lim.acquire("waiter"))
    await asyncio.sleep(0.01)
    assert lim.snapshot()["queued_total"] == 1

    await lim.release(priority=first[0], lease=first[1])
    await asyncio.sleep(0.01)
    assert not parked.done(), "occupancy is still at the cap; nothing dispatches"

    await lim.release(priority=second[0], lease=second[1])
    await asyncio.wait_for(parked, timeout=1.0)
    assert lim.snapshot()["inflight"] == 1
    await lim.release()


def test_mixed_fair_and_bypass_lane_reports_bypass() -> None:
    """One bearer that never queues makes the lane's bound advisory."""
    fair = FairBearerLimiter(2, "fair")
    fair.max_concurrent = 2
    bypass = FairBearerLimiter(2, "off")
    block = proxy._lane_saturation(
        {"a": {"limiter": fair.snapshot()}, "b": {"limiter": bypass.snapshot()}},
        {"a": True, "b": True},
    )
    inputs = block["queue_admit"]
    assert inputs["bypass_bearers"] == 1
    assert inputs["enforced"] is False
    assert inputs["source"] == "bypass"
    assert inputs["fair_source"] == "cold"
    assert block["queue_admit_max_depth"] > 0


def test_hot_tuned_knob_rejects_a_non_finite_float() -> None:
    """`nan` compares false against both bounds and would sail past them."""
    spec = config.EDITABLE_KNOBS["queue_max_wait_s"]
    with pytest.raises(ValueError):
        config._coerce(spec, "nan")
    with pytest.raises(ValueError):
        config._coerce(spec, "1e400")
    assert config._coerce(spec, "45") == 45.0


def test_published_depth_is_exact_not_a_simulation_cap() -> None:
    """A step cap both breaks the health budget and lies about capacity.

    32 idle slots at 1 ms service really can start 5.76M requests inside a
    180 s bound; publishing "1023" is a number a consumer would act on.
    """
    from anthropic_throttle_proxy.limiter import _admissible_ahead

    assert _admissible_ahead([0.0] * 32, 0.001, 180.0) == 5_760_031
    assert _admissible_ahead([0.0, 0.0], 10.0, 30.0) == 7
    assert _admissible_ahead([0.0, 25.0], 10.0, 30.0) == 4  # 0/10/20/30 + 25
    assert _admissible_ahead([31.0], 10.0, 30.0) == 0  # nothing starts in time
    assert _admissible_ahead([], 10.0, 30.0) == 0


def test_snapshot_stays_cheap_for_a_wide_registry() -> None:
    """`/__throttle/health` renders one snapshot per bearer under a 50 ms budget."""
    import time

    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = 32
    lim.snapshot()  # warm
    started = time.perf_counter()
    for _ in range(250):
        lim.snapshot()
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 50, f"250 bearer snapshots took {elapsed_ms:.1f} ms"
