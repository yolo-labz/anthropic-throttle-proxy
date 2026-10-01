#!/usr/bin/env python3
"""Synthetic seat-fairness / capacity check for spec 282.

SIMULATION ONLY — this drives the in-repo ``FairBearerLimiter`` with synthetic
service times and a synthetic seat roster. It is NOT a provider benchmark: no
real inference, no account access, no window extrapolation, no runway claim.

Phases (all deterministic where asserted):
  1. client round-robin fairness        -> spec F1/F2 (real limiter)
  2. equal-eligible capacity, asymmetric service/caps, one unavailable seat
                                          -> spec C5; T1 measurements.
     The roster/assignment is a TOY scheduler: it illustrates the contract
     (F3-compatible) but is NOT production-router proof — the dedicated 284
     test suite owns actual handler/router evidence.
  3. cancellation accounting            -> spec F4 (real limiter)
  4. bounded queue wait                 -> spec F5 (real limiter)
  5. AIMD floor under repeated shrink   -> spec F6 (real limiter)
  6. accounting model functions         -> spec C1/C2/C3/C5; T2/T3 model.
     These helpers model the contract; they are NOT guards in the running
     proxy. Concurrency slots, subscription quota and throughput are distinct
     dimensions throughout.

Usage:  uv run python scripts/check-seat-fairness.py
Exit:   0 = every invariant held; 1 = at least one failed (each named below).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import statistics
import sys
import time
from pathlib import Path

# Pin the AIMD knobs BEFORE importing config so the live cap is deterministic:
# live cap starts at AIMD_INITIAL_CONCURRENT (bounded by hard_max / AIMD_MIN).
# The dynamic import (not `import x` statements) keeps these pins load-bearing
# without moving imports below code.
os.environ["THROTTLE_AIMD_INITIAL_CONCURRENT"] = "2"
os.environ["THROTTLE_AIMD_MIN"] = "1"
os.environ["THROTTLE_PRIORITY_RESERVE_SLOTS"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

config = importlib.import_module("anthropic_throttle_proxy.config")
_limiter_mod = importlib.import_module("anthropic_throttle_proxy.limiter")
FairBearerLimiter = _limiter_mod.FairBearerLimiter
QueueWaitTimeout = _limiter_mod.QueueWaitTimeout

SVC_MS = {"X": 4.0, "Y": 12.0}  # asymmetric service durations (synthetic)


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.total = 0

    def expect(self, cond: bool, name: str, detail: str = "") -> None:
        self.total += 1
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            self.failures.append(name)


async def record_acquire(lim: FairBearerLimiter, cid: str, order: list[str]) -> None:
    eff, lease = await lim.acquire_lease(cid)
    order.append(cid)
    await lim.release(priority=eff, lease=lease)


async def phase1_rr(check: Check) -> None:
    """A chatty client (8 queued) cannot starve a sibling's backlog (2 queued)."""
    lim = FairBearerLimiter(max_concurrent=1, queue_mode="fair", bearer_id="")
    eff, lease = await lim.acquire_lease("holder")  # saturate the single slot
    order: list[str] = []
    tasks = [
        asyncio.create_task(record_acquire(lim, cid, order)) for cid in ["A"] * 8 + ["B"] * 2
    ]  # fixed arrival order: A first
    await asyncio.sleep(0)  # every task enqueues before the holder releases
    await lim.release(priority=eff, lease=lease)
    await asyncio.gather(*tasks)
    check.expect(
        order[:4] == ["A", "B", "A", "B"],
        "F1 RR interleaves 1-for-1",
        f"first dispatches: {order[:4]}",
    )
    check.expect(
        order.count("B") == 2 and max(i for i, c in enumerate(order) if c == "B") < 4,
        "F2 sibling backlog not starved (B completes within first 2k=4 dispatches)",
        f"order={order}",
    )
    check.expect(lim.inflight == 0 and not lim._queues, "phase1 accounting clean")


async def phase2_capacity(check: Check) -> None:
    """Equal-eligible servers, asymmetric service/caps, one unavailable seat.

    Illustrative scheduling only: the toy roster/assignment demonstrates the
    contract's accounting shape, NOT production-router exclusion of Z (that
    evidence belongs to the dedicated 284 test suite).
    """
    seats = {
        "X": FairBearerLimiter(max_concurrent=2, queue_mode="fair", bearer_id=""),
        "Y": FairBearerLimiter(max_concurrent=1, queue_mode="fair", bearer_id=""),
    }
    roster = {"X": "fresh", "Y": "fresh", "Z": "stale"}  # Z unavailable -> ineligible

    def eligible() -> list[str]:
        return [s for s, w in roster.items() if w == "fresh"]

    check.expect(
        eligible() == ["X", "Y"], "C5 denominator excludes stale seats", f"eligible={eligible()}"
    )

    completed = {"X": 0, "Y": 0, "Z": 0}
    running = {"X": 0, "Y": 0}
    overlap_max = {"X": 0, "Y": 0}
    waits: list[float] = []
    busy_ns = {"X": 0, "Y": 0}

    async def job(seat: str) -> None:
        t0 = time.monotonic()
        lim = seats[seat]
        eff, lease = await lim.acquire_lease(f"client-{seat}")
        waits.append(time.monotonic() - t0)
        running[seat] += 1
        overlap_max[seat] = max(overlap_max[seat], running[seat])
        t1 = time.monotonic()
        await asyncio.sleep(SVC_MS[seat] / 1000.0)
        busy_ns[seat] += time.monotonic() - t1
        running[seat] -= 1
        completed[seat] += 1
        await lim.release(priority=eff, lease=lease)

    t0 = time.monotonic()
    assignments = [eligible()[i % len(eligible())] for i in range(12)]  # equal raw split
    await asyncio.gather(*[job(s) for s in assignments])
    wall = time.monotonic() - t0

    check.expect(
        completed == {"X": 6, "Y": 6, "Z": 0},
        "F3 (illustrative) toy scheduler routes zero work to unavailable seat",
        f"not production-router proof; completed={completed}",
    )
    check.expect(
        overlap_max["X"] == 2 and overlap_max["Y"] == 1,
        "effective overlap == live cap on each seat",
        f"overlap_max={overlap_max}",
    )
    check.expect(all(lim.inflight == 0 for lim in seats.values()), "phase2 accounting clean")

    # Concurrency-share normalization vs equal raw requests (spec §2).
    # DIMENSIONS: these are CONCURRENCY slots — not subscription quota.
    conc_share = {"X": 2 / 3, "Y": 1 / 3}  # Z contributes nothing (C5)
    work_share = {"X": 6 / 12, "Y": 6 / 12}
    norm = {s: work_share[s] / conc_share[s] for s in ("X", "Y")}
    check.expect(
        abs(sum(conc_share.values()) - 1.0) < 1e-9 and "Z" not in conc_share,
        "C5 eligible-concurrency denominator excludes stale seats",
    )
    check.expect(
        norm["X"] < norm["Y"],
        "equal raw split is NOT equal concurrency-normalized utilization",
        f"norm={ {k: round(v, 3) for k, v in norm.items()} }",
    )

    mean_overlap = sum(busy_ns.values()) / wall
    print(
        f"  [info] SIMULATION wall={wall * 1000:.1f}ms completed=12 "
        f"(illustrative toy scheduler) mean_overlap={mean_overlap:.2f} queue_wait "
        f"p50={statistics.median(waits) * 1000:.2f}ms max={max(waits) * 1000:.2f}ms "
        f"busy={ {k: round(v * 1000, 1) for k, v in busy_ns.items()} }ms"
    )


async def phase3_cancel(check: Check) -> None:
    """A cancelled waiter never acquires and leaks no slot/lease."""
    lim = FairBearerLimiter(max_concurrent=1, queue_mode="fair", bearer_id="")
    eff, lease = await lim.acquire_lease("holder")
    got: list[str] = []

    async def waiter(cid: str) -> None:
        eff2, lease2 = await lim.acquire_lease(cid)
        got.append(cid)
        await lim.release(priority=eff2, lease=lease2)

    t_a = asyncio.create_task(waiter("c1"))
    t_b = asyncio.create_task(waiter("c1-cancelled"))
    t_c = asyncio.create_task(waiter("c2"))
    await asyncio.sleep(0)
    t_b.cancel()
    try:
        await t_b
    except asyncio.CancelledError:
        pass
    await lim.release(priority=eff, lease=lease)
    await asyncio.gather(t_a, t_c)
    check.expect("c1-cancelled" not in got, "F4 cancelled waiter never acquired", f"got={got}")
    check.expect(got == ["c1", "c2"], "F4 queue order preserved after cancel", f"got={got}")
    check.expect(
        lim.inflight == 0 and not lim._holds and not lim._queues,
        "F4 no slot/lease leak after cancel",
    )


async def phase4_bounded_wait(check: Check) -> None:
    """QueueWaitTimeout answers with no slot held (clean 503 shape upstream)."""
    lim = FairBearerLimiter(max_concurrent=1, queue_mode="fair", bearer_id="")
    eff, lease = await lim.acquire_lease("holder")
    raised = False
    try:
        async with lim.slot("bounded", max_wait=0.03):
            pass
    except QueueWaitTimeout:
        raised = True
    check.expect(raised, "F5 bounded wait raises QueueWaitTimeout")
    check.expect(
        lim.inflight == 1 and len(lim._holds) == 1,
        "F5 no slot held by the rejected request",
        f"inflight={lim.inflight}",
    )
    await lim.release(priority=eff, lease=lease)
    check.expect(lim.inflight == 0 and not lim._holds, "F5 lease bookkeeping clean")


async def phase5_aimd_floor(check: Check) -> None:
    """Repeated shrink never empties the lane (AIMD floor >= 1)."""
    lim = FairBearerLimiter(max_concurrent=8, queue_mode="fair", bearer_id="")
    caps = [lim.max_concurrent]
    for _ in range(6):
        new = await lim.shrink()
        caps.append(new)
    check.expect(
        all(c is not None and c >= config.AIMD_MIN for c in caps),
        "F6 shrink never drops below AIMD_MIN",
        f"caps={caps}",
    )
    check.expect(
        caps[-1] == config.AIMD_MIN == 1, "F6 floor reached and stable", f"final={caps[-1]}"
    )


def aggregate_window_percent(readings: list[tuple[str, str, float, float]]) -> float:
    """C1 model: aggregate ONLY as sum(used)/sum(limit) with strict identity.

    Each reading is (window, unit, used, limit). Aggregation is refused unless
    every reading shares ONE window identity AND ONE unit; bare percentages
    (no denominator) are unbound and must never be aggregated at all. Equal
    window labels alone do not prove common boundaries, units or denominators.
    """
    identities = {(w, u) for w, u, _used, _limit in readings}
    if len(identities) != 1:
        raise ValueError(f"refusing to aggregate across identities {sorted(identities)}")
    if any(limit <= 0 for _w, _u, _used, limit in readings):
        raise ValueError("unbound or non-positive denominator cannot be aggregated")
    used = sum(used for _w, _u, used, _limit in readings)
    limit = sum(lim for _w, _u, _used, lim in readings)
    return 100.0 * used / limit


def eligible_slots(roster: dict[str, dict[str, object]]) -> int:
    """C3/C5 model: eligible CONCURRENCY slots — a distinct dimension from
    subscription quota. Unassigned/stale/unknown/exhausted seats contribute 0.
    """
    return sum(int(s["cap"]) for s in roster.values() if s["assigned"] and s["state"] == "fresh")


def tuning_admissible(
    *,
    overlap: float,
    cap: int,
    queue_wait_p95_s: float,
    pushbacks_429: int,
    queue_timeouts: int,
    mixed_window_reading: bool,
) -> bool:
    """T2/T3 model (not a running-system guard): a higher cap is admissible only
    with evidence; halt on 429/timeouts/mixed-window readings.
    """
    if mixed_window_reading:
        return False  # C1 violation: halt
    if pushbacks_429 or queue_timeouts:
        return False  # T3 halt conditions
    return overlap >= cap and queue_wait_p95_s > 0.25  # seats binding, demand present


def phase6_accounting(check: Check) -> None:
    # C1 dimension check: equal-size accounts at 40% and 30% aggregate to 35%
    # of their combined allowance (sum(used)/sum(limit)) — NEVER 70.
    got = aggregate_window_percent([("5h", "req", 40.0, 100.0), ("5h", "req", 30.0, 100.0)])
    check.expect(
        abs(got - 35.0) < 1e-9,
        "C1 equal accounts 40%/30% aggregate to 35% (weighted), never 70",
        f"got={got}",
    )
    for name, readings in [
        (
            "C1 mixed-window aggregation refused",
            [("5h", "req", 40.0, 100.0), ("7d", "req", 30.0, 100.0)],
        ),
        (
            "C1 mixed-unit aggregation refused",
            [("5h", "req", 40.0, 100.0), ("5h", "usd", 30.0, 100.0)],
        ),
        ("C1 unbound denominator refused", [("5h", "req", 40.0, 0.0), ("5h", "req", 30.0, 0.0)]),
    ]:
        try:
            aggregate_window_percent(readings)
            check.expect(False, name)
        except ValueError:
            check.expect(True, name)
    roster = {
        "a": {"cap": 2, "assigned": True, "state": "fresh"},
        "b": {"cap": 1, "assigned": True, "state": "stale"},  # stale
        "c": {"cap": 1, "assigned": True, "state": "unknown"},  # unknown
        "d": {"cap": 4, "assigned": False, "state": "fresh"},  # unassigned purchase
        "e": {"cap": 1, "assigned": True, "state": "exhausted"},  # exhausted
    }
    check.expect(
        eligible_slots(roster) == 2,
        "C3/C5 excluded seats contribute no eligible slots "
        "(concurrency slots, NOT subscription quota)",
        f"eligible_slots={eligible_slots(roster)}",
    )
    check.expect(
        not tuning_admissible(
            overlap=1.0,
            cap=2,
            queue_wait_p95_s=1.0,
            pushbacks_429=0,
            queue_timeouts=0,
            mixed_window_reading=False,
        ),
        "T1 cap raise without measured overlap is not admissible",
    )
    check.expect(
        not tuning_admissible(
            overlap=2.0,
            cap=2,
            queue_wait_p95_s=1.0,
            pushbacks_429=1,
            queue_timeouts=0,
            mixed_window_reading=False,
        ),
        "T3 halt on any 429",
    )


async def main() -> int:
    print("== seat-fairness / capacity check — SIMULATION (not a provider benchmark) ==")
    check = Check()
    for name, fn in [
        ("phase1 client round-robin (real limiter)", phase1_rr),
        ("phase2 equal-eligible capacity (illustrative scheduling)", phase2_capacity),
        ("phase3 cancellation (real limiter)", phase3_cancel),
        ("phase4 bounded wait (real limiter)", phase4_bounded_wait),
        ("phase5 AIMD floor (real limiter)", phase5_aimd_floor),
    ]:
        print(f"-- {name}")
        await fn(check)
    print("-- phase6 accounting model functions (not running-system guards)")
    phase6_accounting(check)
    if check.failures:
        print(
            f"\nRESULT: FAIL — {check.total - len(check.failures)}/{check.total} "
            f"assertions hold (simulation); broken: {check.failures}"
        )
        return 1
    print(f"\nRESULT: PASS — {check.total}/{check.total} assertions hold (simulation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
