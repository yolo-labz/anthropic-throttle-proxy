"""Per-bearer concurrency limiter with weighted-fair-queueing + AIMD ceiling.

Replaces a plain ``asyncio.Semaphore``. Same in-flight cap, but queued
requests are dispatched round-robin across distinct ``client_id``s so no
client can monopolize slots under sustained backlog, and the live ceiling
shrinks/grows reactively (AIMD) on upstream rate pushback.
"""

from __future__ import annotations

import asyncio
import collections
import heapq
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config
from .config import log
from .metrics import M_AIMD_MAX

# Completed slot durations kept per lane. Small on purpose: the estimator wants
# what this lane is doing NOW, not its lifetime average.
_SERVICE_SAMPLES = 64
# Below this many completions the lane has not measured itself yet and the
# configured cold default is used instead.
_MIN_SERVICE_SAMPLES = 3
# Nearest-rank quantile of recent service times. p90 rather than the median:
# admission is a safety bound, and under-estimating the lane's service time is
# what parks a request that cannot be drained.
_SERVICE_QUANTILE = 0.9
# Floor on the estimated service time. Zero would make every depth admissible
# while publishing a zero bound — two contradictory answers from one number.
_MIN_SERVICE_TIME_S = 0.001
# Upper bound on simulated dispatch events per drain estimate. Bounds both the
# per-request check and the published bound; health calls this per bearer.
_DRAIN_SIM_STEPS = 1024
# Held-slot bookkeeping is keyed by lease, so it is bounded by real in-flight
# count. This cap only exists so a leaked lease (a bug) degrades the estimate
# instead of growing memory forever — the #205 failure shape.
_HOLDS_SOFT_CAP = 4096

# Late-bound in main() (needs the running loop) — guards the registry below.
bearer_limiter_lock: asyncio.Lock | None = None
_retry_after_state: dict[str, float] | None = None


class _RetryProbeGate:
    """Event-loop-local half-open gate for one bearer's long retry window."""

    __slots__ = ("block_while_retry", "event", "inflight", "required")

    def __init__(self, *, block_while_retry: bool) -> None:
        self.required = True
        self.inflight = False
        self.block_while_retry = block_while_retry
        self.event: asyncio.Event | None = None


# Synchronous on purpose: handler routing and probe claim run without an await
# between them, so one event-loop task wins the half-open lease atomically.
_retry_probe_gates: dict[str, _RetryProbeGate] = {}


def require_retry_probe(bid: str, *, block_while_retry: bool = False) -> None:
    if not bid:
        return
    gate = _retry_probe_gates.get(bid)
    if gate is None:
        _retry_probe_gates[bid] = _RetryProbeGate(block_while_retry=block_while_retry)
    else:
        if gate.required:
            gate.block_while_retry = gate.block_while_retry or block_while_retry
        else:
            gate.block_while_retry = block_while_retry
        gate.required = True


def retry_probe_required(bid: str) -> bool:
    gate = _retry_probe_gates.get(bid)
    return bool(gate and gate.required)


def retry_probe_inflight(bid: str) -> bool:
    gate = _retry_probe_gates.get(bid)
    return bool(gate and gate.required and gate.inflight)


def retry_probe_blocks_routing(bid: str) -> bool:
    gate = _retry_probe_gates.get(bid)
    return bool(gate and gate.required and gate.block_while_retry)


def try_begin_retry_probe(bid: str) -> bool:
    gate = _retry_probe_gates.get(bid)
    if gate is None or not gate.required or gate.inflight:
        return False
    gate.inflight = True
    if gate.event is None or gate.event.is_set():
        gate.event = asyncio.Event()
    log(f"retry-probe-begin bid={bid}")
    return True


async def wait_retry_probe(bid: str) -> None:
    """Wait until the current half-open probe resolves, if one is active."""
    while True:
        gate = _retry_probe_gates.get(bid)
        if gate is None or not gate.required or not gate.inflight:
            return
        if gate.event is None:
            gate.event = asyncio.Event()
        await gate.event.wait()


def finish_retry_probe(bid: str, *, success: bool) -> bool:
    """Release a probe lease; only a successful message response reopens."""
    gate = _retry_probe_gates.get(bid)
    if gate is None or not gate.inflight:
        return False
    gate.inflight = False
    if success:
        gate.required = False
        gate.block_while_retry = False
    if gate.event is not None:
        gate.event.set()
    log(f"retry-probe-finish bid={bid} result={'open' if success else 'closed'}")
    return True


def clear_retry_probe(bid: str) -> bool:
    """Disarm a bearer's half-open gate outright — no probe, no lease.

    ``finish_retry_probe`` only disarms on SUCCESS, which is the right rule for
    a rate-limited bearer: it stays gated until the upstream actually serves it
    again. A bearer whose CREDENTIAL is refused can never produce that success,
    so its gate stays armed forever and keeps electing real client turns as
    probes that are guaranteed to fail. Credential quarantine disarms the gate
    instead; the synthetic re-check owns re-testing from there.

    Anyone parked in ``wait_retry_probe`` is woken before the gate is dropped —
    the loop re-reads the registry, finds no gate, and returns.
    """
    gate = _retry_probe_gates.pop(bid, None)
    if gate is None:
        return False
    if gate.event is not None:
        gate.event.set()
    return True


def probe_inflight_bids() -> list[str]:
    """Bearers whose half-open probe is currently held by some other request.

    Routing scores such a bearer ``inf`` (see ``_account_routing_candidate_score``),
    so for the lifetime of one probe a fleet can look candidate-less even though
    a healthy account exists. Callers use this to park on the probe instead of
    failing the request outright.
    """
    return [bid for bid, gate in _retry_probe_gates.items() if gate.required and gate.inflight]


def _reset_retry_probe_gates() -> None:
    """Test-only registry reset alongside the other process-global state."""
    _retry_probe_gates.clear()


def set_lock(lock: asyncio.Lock) -> None:
    """Bind the registry lock (called once from ``main()`` on the running loop)."""
    global bearer_limiter_lock
    bearer_limiter_lock = lock


def _initial_live_cap(hard_max: int) -> int:
    """Initial AIMD live cap for a new bearer, bounded by the hard ceiling."""
    return min(hard_max, max(config.AIMD_MIN, config.AIMD_INITIAL_CONCURRENT))


def _retry_after_state_path() -> Path | None:
    raw = config.RETRY_AFTER_STATE_FILE
    return Path(os.path.expanduser(raw)) if raw else None


def _load_retry_after_state() -> dict[str, float]:
    global _retry_after_state
    if _retry_after_state is not None:
        return _retry_after_state
    path = _retry_after_state_path()
    if path is None:
        _retry_after_state = {}
        return _retry_after_state
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _retry_after_state = {}
        return _retry_after_state
    if not isinstance(raw, dict):
        _retry_after_state = {}
        return _retry_after_state
    now = time.time()
    cap = config.RETRY_AFTER_RESTORE_CAP_S
    state: dict[str, float] = {}
    capped_any = False
    for bid, until in raw.items():
        if not isinstance(until, (int, float)) or float(until) <= now:
            continue
        capped = float(until)
        if cap > 0 and capped > now + cap:
            log(
                f"retry-after-restore-capped bid={bid} "
                f"orig_remaining={int(capped - now)}s cap={int(cap)}s"
            )
            capped = now + cap
            capped_any = True
        state[str(bid)] = capped
    _retry_after_state = state
    if capped_any:
        # Persist the capped deadlines so a restart does not re-grant a fresh
        # stale window from the original multi-day value on disk each time.
        _persist_retry_after_state()
    return _retry_after_state


def _persist_retry_after_state() -> None:
    path = _retry_after_state_path()
    if path is None:
        return
    state = _load_retry_after_state()
    now = time.time()
    live = {bid: until for bid, until in state.items() if until > now}
    _retry_after_state.clear()
    _retry_after_state.update(live)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(live, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log(f"retry-after-state-write-error path={path} err={exc!r}")


def _persist_retry_after_until(bid: str, until: float) -> None:
    if not bid or _retry_after_state_path() is None:
        return
    state = _load_retry_after_state()
    state[bid] = until
    _persist_retry_after_state()


def _restore_retry_after(lim: FairBearerLimiter, bid: str) -> None:
    until = _load_retry_after_state().get(bid, 0.0)
    if until <= time.time():
        return
    lim._retry_after_until = until
    lim.require_retry_probe(block_while_retry=True)
    lim._last_throttle_at = max(lim._last_throttle_at, time.time())
    log(f"bearer-retry-after-restore bid={bid} remaining={int(until - time.time())}s")


def clear_retry_after(bid: str) -> float:
    """Drop any persisted + live Retry-After window for ``bid``.

    Called with fresh evidence that the window no longer matches reality
    (e.g. the usage endpoint reports the account back below exhaustion).
    Returns the remaining seconds that were cleared (0 when nothing to do).
    Reads ``config.bearer_limiters`` without the registry lock — same as the
    metrics collectors — and only zeroes a float attribute, never mutates
    the dict.
    """
    now = time.time()
    state = _load_retry_after_state()
    until = float(state.pop(bid, 0.0) or 0.0)
    lim = config.bearer_limiters.get(bid)
    if lim is not None:
        until = max(until, lim._retry_after_until)
        lim._retry_after_until = 0.0
    if until <= now:
        return 0.0
    if until - now > config.MAX_HOLD_RETRY_AFTER_S:
        if lim is None:
            require_retry_probe(bid, block_while_retry=True)
        else:
            lim.require_retry_probe(block_while_retry=True)
    _persist_retry_after_state()
    return until - now


async def _retune_limiter_hard_max(
    bid: str,
    lim: FairBearerLimiter,
    hard_max: int,
    *,
    live_floor: int | None = None,
) -> None:
    """Apply an operator hard-ceiling change to an existing limiter."""
    if hard_max == lim.hard_max and live_floor is None:
        return
    async with lim._lock:
        old_hard_max = lim.hard_max
        lim.hard_max = hard_max
        if hard_max < old_hard_max:
            lim.max_concurrent = min(lim.max_concurrent, hard_max)
        if live_floor is not None:
            lim.max_concurrent = max(
                lim.max_concurrent,
                min(hard_max, max(config.AIMD_MIN, live_floor)),
            )
        # Increasing the hard ceiling alone must not jump the live cap. AIMD
        # should discover new safe ceiling by traffic. An explicit live_floor is
        # different: that is the operator raising the AIMD warm-start cap.
        if lim.queue_enabled:
            lim._try_dispatch()
        M_AIMD_MAX.labels(bearer=bid).set(lim.max_concurrent)
        log(f"bearer-retune bid={bid} hard_max={hard_max} max_concurrent={lim.max_concurrent}")


async def retune_existing_limiters(hard_max: int, *, live_floor: int | None = None) -> None:
    """Retune every already-allocated bearer limiter to a new hard ceiling."""
    async with bearer_limiter_lock:
        limiters = list(config.bearer_limiters.items())
    for bid, lim in limiters:
        await _retune_limiter_hard_max(bid, lim, hard_max, live_floor=live_floor)


async def kick_existing_limiters() -> None:
    """Re-run dispatch on every allocated limiter after a knob retune.

    Queued waiters only wake on acquire/release events; a hot-tune that
    changes dispatch math (e.g. PRIORITY_RESERVE_SLOTS raised, or lowered to
    0 which migrates queued lane waiters to the normal queue) must kick the
    loop itself or already-parked futures sit stranded until unrelated
    traffic arrives (Codex round-2 MAJOR on PR #73).
    """
    if bearer_limiter_lock is not None:
        async with bearer_limiter_lock:
            limiters = list(config.bearer_limiters.values())
    else:
        # Lock is wired in proxy.main(); before that (unit tests) the registry
        # is only touched from one task, so a bare snapshot is safe.
        limiters = list(config.bearer_limiters.values())
    for lim in limiters:
        async with lim._lock:
            lim._try_dispatch()


@dataclass(frozen=True)
class DrainEstimate:
    """Can this lane still drain one more queued request inside the budget?

    Every field is a live lane fact or arithmetic over live lane facts, so the
    answer is reproducible from ``/__throttle/admission`` without re-deriving
    it (the duplicate-oracle failure this repo already documents for
    availability).

    ``max_depth`` is the largest queue depth still admissible under
    ``max_wait_s``; it is advisory when ``enforced`` is false (bound disabled),
    where depth is in fact unlimited.

    ``admits`` is pure arithmetic. ``rejects`` is the only thing the hot path
    acts on, and it additionally requires ``evidenced``: a lane that has never
    completed a request and holds no slot longer than the configured cold
    default has GUESSED its service time, and a guess must not turn traffic
    away. Evidence arrives within the first few requests (three completions,
    or one slot held past the cold default), which is long before a queue can
    grow into the shape this guard exists for.
    """

    slots: int
    busy: int
    free: int
    queued: int
    ahead: int
    service_time_s: float
    residual_s: float
    samples: int
    source: str
    evidenced: bool
    max_wait_s: float
    enforced: bool
    rounds: int
    wait_s: float
    max_depth: int
    admits: bool
    rejects: bool
    retry_after_s: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _quantile(values: list[float], q: float) -> float:
    """Nearest-rank quantile of an unsorted list. ``values`` must be non-empty."""
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def _finite(value: float, fallback: float) -> float:
    """``value`` when it is a real finite number, else ``fallback``.

    An inherited wait budget arrives as a client-supplied header and a config
    default arrives from the environment; either can parse to ``inf`` or
    ``nan``, which turns the arithmetic below into an ``OverflowError`` on a
    request path (and into invalid JSON on ``/__throttle/health``).
    """
    number = float(value)
    return number if math.isfinite(number) else fallback


def _service_estimate(
    samples: collections.deque[float],
    holds: list[float],
    now: float | None = None,
) -> tuple[float, float, int, str]:
    """Conservative seconds-per-request for one lane, with its provenance.

    Two independent sources, and the larger wins:

    * completed requests (p90 of the recent window) once the lane has measured
      itself at least ``_MIN_SERVICE_SAMPLES`` times, else the cold default;
    * how long the OLDEST currently-held slot has already been held. That age
      is a lower bound on that request's TOTAL service time — not on its
      remaining time — and it is the only signal available in the shape that
      motivated this code: at 14:20 on 01/09/2026 the two slow generations had
      not completed yet, so a completion-only estimator was blind to exactly
      the requests doing the blocking.

    Also returns a PER-HOLDER residual: how much longer each currently-held
    slot is modelled to run. A slot younger than a typical request has that
    much of a typical request left, which keeps an arrival behind a
    nearly-finished slot from being refused over time it will not wait. A slot
    that is already OVERDUE has an unknown remainder, and service times here
    are heavy-tailed — a 200 s generation is likelier to keep running than a
    2 s one — so it is priced at a full estimated service rather than at zero.

    Per holder, not a single scalar: two slots aged 200 s and 34 s free at very
    different times, and collapsing them either lets an arrival through on the
    nearly-done one (when the other is what it will actually wait for) or
    refuses it on the stuck one (when the other is about to free). Both were
    reachable with one number (Codex round-2 BLOCKER).
    """
    stamp = time.monotonic() if now is None else now
    count = len(samples)
    if count >= _MIN_SERVICE_SAMPLES:
        typical, source = _quantile(list(samples), _SERVICE_QUANTILE), "measured"
    else:
        typical, source = _finite(config.QUEUE_DRAIN_DEFAULT_S, 10.0), "cold"
    typical = max(_MIN_SERVICE_TIME_S, typical)
    service = typical
    ages = sorted(max(0.0, stamp - started) for started in holds)
    if ages and ages[-1] > service:
        # A slot held longer than any completed request IS evidence the lane is
        # slower than its history says.
        service, source = ages[-1], "inflight"
    service = max(_MIN_SERVICE_TIME_S, service)
    residuals = [typical - age if age < typical else service for age in ages]
    return service, residuals, count, source


def _wait_for_position(available: list[float], service_time_s: float, ahead: int) -> float:
    """When the ``ahead + 1``-th dispatch starts, given each slot's free time.

    Greedy earliest-slot assignment over a min-heap of "this slot is free at
    T" — the same order the dispatcher produces. Closed-form round arithmetic
    cannot express it: with slots free at 0 s and 100 s and a 1 s service, the
    first slot serves a hundred requests before the second serves one.

    Beyond ``_DRAIN_SIM_STEPS`` the schedule is regular enough that the
    remaining requests are priced as full rounds; a lane that deep is far past
    any wait bound anyway.
    """
    if not available:
        return math.inf
    heap = list(available)
    heapq.heapify(heap)
    simulated = min(ahead, _DRAIN_SIM_STEPS)
    for _ in range(simulated):
        heapq.heappush(heap, heapq.heappop(heap) + service_time_s)
    start = heap[0]
    if ahead > simulated:
        start += math.ceil((ahead - simulated) / len(available)) * service_time_s
    return start


def _admissible_ahead(available: list[float], service_time_s: float, horizon: float) -> int:
    """Largest ``ahead`` whose dispatch still starts within ``horizon``.

    Counts the dispatches that begin inside the horizon on the same schedule;
    the last of them is the arrival itself, hence ``count - 1``. Saturates at
    ``_DRAIN_SIM_STEPS``: a lane that can drain a thousand queued requests
    inside its wait bound is not the failure this guard exists for.
    """
    if not available:
        return 0
    heap = list(available)
    heapq.heapify(heap)
    count = 0
    while count < _DRAIN_SIM_STEPS and heap[0] <= horizon:
        heapq.heappush(heap, heapq.heappop(heap) + service_time_s)
        count += 1
    return max(0, count - 1)


def _compute_drain(
    *,
    slots: int,
    busy: int,
    queued: int,
    ahead: int | None = None,
    service_time_s: float,
    residuals: list[float] | None = None,
    samples: int,
    source: str,
    evidenced: bool,
    max_wait: float | None,
) -> DrainEstimate:
    """Queueing arithmetic for one arriving request. Pure — no limiter state.

    ``ahead`` is how many queued requests will be dispatched BEFORE this
    arrival. Under the limiter's per-client round-robin that is NOT the total
    queue depth: a new client with one request overtakes a chatty client's
    backlog by design, which is the whole point of the fair queue. Callers pass
    the round-robin-aware count; ``None`` falls back to ``queued`` (strict
    FIFO), which is the conservative reading used when no client is named.

    Each server is modelled as "free at T": idle slots now, held slots after
    their own ``residuals`` entry. Requests take the earliest free slot, which
    is what the dispatcher does, and the arrival's start time is the
    ``ahead + 1``-th such event. Per-holder residuals matter: two slots aged
    200 s and 34 s free at very different times, and one scalar for the pair
    either lets an arrival through on the nearly-done slot or refuses it on
    the stuck one.

    This is a conservative POLICY, not a proof. A holder's age bounds its total
    service time from below, never its remaining time; it may return in the
    next millisecond. The model deliberately errs toward over-estimating the
    wait (a false refusal answered in milliseconds, which the SDK retries)
    rather than under-estimating it (a request parked past the client's
    patience, answered too late to be useful — the failure being fixed).
    """
    slots = max(0, int(slots))
    busy = max(0, int(busy))
    queued = max(0, int(queued))
    ahead = queued if ahead is None else max(0, int(ahead))
    service_time_s = max(_MIN_SERVICE_TIME_S, _finite(service_time_s, _MIN_SERVICE_TIME_S))
    horizon = 0.0 if not max_wait or max_wait < 0 else _finite(max_wait, 0.0)
    enforced = horizon > 0.0

    free = max(0, slots - busy)
    held = [
        min(max(0.0, _finite(value, service_time_s)), service_time_s) for value in (residuals or [])
    ][:busy]
    if len(held) < min(busy, slots):
        # No per-slot evidence (a synthetic estimate, or a slot taken before
        # leases existed): price the unknown holders at a full service.
        held += [service_time_s] * (min(busy, slots) - len(held))
    available = [0.0] * free + held

    wait_s = _wait_for_position(available, service_time_s, ahead)
    admits = (not enforced) or (slots > 0 and wait_s <= horizon)
    rejects = enforced and evidenced and not admits
    max_depth = _admissible_ahead(available, service_time_s, horizon) if enforced else 0

    # Coarse descriptor kept for logs and dashboards: how many service rounds
    # deep the arrival is. The wait above is the scheduled answer, not this.
    rounds = (
        0 if ahead + 1 <= free else math.ceil((ahead + 1 - free) / slots) if slots else ahead + 1
    )
    residual = min(held) if held else 0.0
    if not math.isfinite(wait_s):
        # Only reachable with zero servers: nothing will ever dispatch.
        wait_s = horizon + service_time_s

    # The advertised retry is the drain estimate, never shorter than the
    # historical constant, and HARD-capped: an unbounded hint is not actionable.
    # The pre-queue rejection path raises this floor to the budget it just
    # refused (still under the cap) — a shorter hint would send a compliant
    # client straight back into the same wall, which is the measured x4 retry
    # storm. The elapsed path does not, because that budget is already spent.
    retry_after_s = min(
        config.QUEUE_RETRY_AFTER_MAX_S,
        max(config.QUEUE_TIMEOUT_RETRY_AFTER_S, math.ceil(wait_s)),
    )

    return DrainEstimate(
        slots=slots,
        busy=busy,
        free=free,
        queued=queued,
        ahead=ahead,
        service_time_s=round(service_time_s, 3),
        residual_s=round(residual, 3),
        samples=samples,
        source=source,
        evidenced=evidenced,
        max_wait_s=round(horizon, 3),
        enforced=enforced,
        rounds=rounds,
        wait_s=round(wait_s, 3),
        max_depth=max_depth,
        admits=admits,
        rejects=rejects,
        retry_after_s=retry_after_s,
    )


def budget_floored_retry_after(estimate: DrainEstimate) -> int:
    """Retry hint for a request refused BEFORE it ever waited.

    It never waited, so the budget it was refused against is still ahead of it:
    advertising less would guarantee an immediate repeat refusal. Still hard-
    capped by ``QUEUE_RETRY_AFTER_MAX_S`` — the ceiling is a ceiling.
    """
    return min(
        config.QUEUE_RETRY_AFTER_MAX_S,
        max(estimate.retry_after_s, math.ceil(estimate.max_wait_s)),
    )


def cold_drain_estimate(max_wait: float | None = None) -> DrainEstimate:
    """The bound a brand-new bearer would get, from config alone.

    ``/__throttle/admission`` must publish a real depth bound even before the
    first request allocates a limiter; a consumer polling a freshly restarted
    proxy would otherwise read "0" and conclude the lane admits nothing.
    """
    return _compute_drain(
        slots=_initial_live_cap(config.MAX_CONCURRENT),
        busy=0,
        queued=0,
        service_time_s=float(config.QUEUE_DRAIN_DEFAULT_S),
        samples=0,
        source="config",
        evidenced=False,
        max_wait=config.QUEUE_MAX_WAIT_S if max_wait is None else max_wait,
    )


class FairBearerLimiter:
    """Per-bearer concurrency limiter with weighted-fair-queueing across clients.

    Same in-flight cap (``max_concurrent``), but queued requests are picked
    round-robin across distinct ``client_id``s so no client can monopolize
    slots even under sustained backlog.

    Old: claude-A queues 50 tool calls just before claude-B queues 1 → B
    waits for ALL 50 of A's calls to drain (Semaphore FIFO acquire order).
    New: A and B interleave 1-for-1 — B's request goes through on the next
    free slot, not after A's entire backlog.
    """

    def __init__(self, max_concurrent: int, queue_mode: str, bearer_id: str = "") -> None:
        # PR #575/PR #40: `hard_max` is the operator-set upper bound (e.g. 32).
        # `max_concurrent` is the LIVE ceiling that starts conservatively, grows
        # after clean traffic, and shrinks on upstream 429/503.
        self.hard_max = max_concurrent
        self.bearer_id = bearer_id
        self.max_concurrent = _initial_live_cap(max_concurrent)
        self.queue_mode = queue_mode
        # PR #580: split queue from observation. `observe` mode bypasses
        # the fair-RR queue (instant slot acquire) but DOES move AIMD
        # counters on 429/503/529 — gives /__throttle/health and the
        # Prometheus dashboard the early-warning signal that `off` loses
        # without re-introducing the queue-stall trade-off.
        self.queue_enabled = queue_mode in {"fair", "reactive"}
        self.observe_enabled = queue_mode != "off"
        self.inflight = 0
        # client_id → deque of waiting futures; _rr_order = client_ids w/ pending work.
        self._queues: dict[str, collections.deque[asyncio.Future]] = {}
        self._rr_order: collections.deque[str] = collections.deque()
        # Priority lane: short/latency-sensitive calls (the /goal Stop-hook
        # evaluator — small max_tokens, no tools) park HERE, not the RR queue,
        # and dispatch against a DEDICATED pool of PRIORITY_RESERVE_SLOTS that
        # is independent of the main pool, so they never starve behind long
        # generations holding every main slot (verified 03/07: a 24s evaluator
        # waited 46s in the FIFO past its 30s client timeout → disconnected →
        # CC "sonnet" error → /goal halts). Independence matters both ways:
        # a post-shrink main pool with stale inflight above the new ceiling
        # cannot pinch the lane shut, and sustained priority load cannot eat
        # main-pool slots (Codex review of PR #73, BLOCKER + starvation MAJOR).
        # Same per-client round-robin structure as the main queue so one chatty
        # client cannot starve a sibling's evaluator inside the lane either.
        self._priority_queues: dict[str, collections.deque[asyncio.Future]] = {}
        self._priority_rr: collections.deque[str] = collections.deque()
        self.priority_inflight = 0
        # Service-time evidence per lane, for queue-DEPTH admission.
        # `_holds` maps a per-slot LEASE to (lane, monotonic dispatch stamp), so
        # a completion prices the request that actually finished. Pairing by
        # arrival order instead would let a stream of short completions pop the
        # stamp of the long request still holding the slot, erasing the one
        # signal the incident turns on. `_samples` holds recent durations.
        self._holds: dict[int, tuple[bool, float]] = {}
        self._next_lease = 0
        self._samples: collections.deque[float] = collections.deque(maxlen=_SERVICE_SAMPLES)
        self._priority_samples: collections.deque[float] = collections.deque(
            maxlen=_SERVICE_SAMPLES
        )
        self._lock = asyncio.Lock()
        # _last_throttle_at: monotonic-ish wall clock of the last shrink.
        # _successes_since_throttle: consecutive 2xx since that shrink.
        # _retry_after_until: wall-clock end of any open Retry-After window.
        self._last_throttle_at = 0.0
        self._successes_since_throttle = 0
        self._retry_after_until = 0.0
        # Cold-start probation is deliberately independent of persisted
        # Retry-After state. A restart in the final seconds of a window (or
        # after its JSON entry was pruned) must still reopen this bearer through
        # one message probe instead of releasing every waiter as a herd.
        if bearer_id:
            require_retry_probe(bearer_id)
        # PR #53: adaptive ramp — sliding window of recent shrink timestamps.
        # _effective_ramp_after() uses this to pick FAST (isolated transient)
        # vs SLOW (sustained storm) additive-increase. ``maxlen`` is sized to
        # config.AIMD_STORM_THRESHOLD_MAX so EVERY valid storm threshold stays
        # reachable: `_recent_shrinks` caps at the deque length, so a maxlen
        # below the threshold ceiling would make storm mode (recent >= threshold)
        # impossible and silently force FAST during real storms. Aged-out entries
        # self-evict, bounding memory under pathological storms.
        self._shrink_history: collections.deque[float] = collections.deque(
            maxlen=config.AIMD_STORM_THRESHOLD_MAX
        )

    def set_queue_mode(self, queue_mode: str) -> None:
        """Switch the limiter's admission mode for future acquires.

        Used by the local tier when central fallback becomes direct-upstream:
        a desktop configured as pass-through while central is healthy must
        still enforce the local fair queue when central is down.
        """
        self.queue_mode = queue_mode
        self.queue_enabled = queue_mode in {"fair", "reactive"}
        self.observe_enabled = queue_mode != "off"

    def slot(
        self, client_id: str, *, priority: bool = False, max_wait: float | None = None
    ) -> _FairSlotContext:
        """Return an async context manager that holds one slot for ``client_id``.

        ``priority=True`` routes a short/latency-sensitive call through the
        reserved latency-lane so it does not starve behind long generations.
        The effective lane is decided once inside :meth:`acquire` (a reserve
        of 0 demotes the call to normal traffic) and echoed back so
        acquire/release accounting stays symmetric for the call's lifetime
        even if the knob is retuned mid-flight.

        ``max_wait`` bounds the QUEUE WAIT only (queue modes): a request still
        parked after that many seconds raises :class:`QueueWaitTimeout` instead
        of stalling past the client's own socket timeout. ``None``/0 keeps the
        historical unbounded wait.
        """
        return _FairSlotContext(self, client_id, priority, max_wait)

    async def shrink(self) -> int | None:
        """AIMD multiplicative-decrease. Called on upstream rate pushback (429/503).

        Multiplies the live ceiling by ``AIMD_DECREASE`` (floor ``AIMD_MIN``),
        records the throttle time, and resets the success counter. Always cuts
        by at least one slot so a fractional decrease can't stall at the same
        value. Already-inflight requests are NOT killed — they finish naturally
        and ``inflight`` drops until it sinks below the new ceiling.

        Returns the new ceiling, or ``None`` in ``off`` mode (no AIMD signal).
        """
        # PR #580: `observe` mode shrinks counters (visible in
        # /__throttle/health + Prometheus) without affecting dispatch.
        # `off` skips entirely — no counter movement, no AIMD signal.
        if not self.observe_enabled:
            return None
        async with self._lock:
            scaled = int(self.max_concurrent * config.AIMD_DECREASE)
            new_max = max(config.AIMD_MIN, min(scaled, self.max_concurrent - 1))
            self.max_concurrent = new_max
            self._last_throttle_at = time.time()
            self._successes_since_throttle = 0
            self._shrink_history.append(self._last_throttle_at)
            return new_max

    def _note_dispatch(self, priority: bool) -> int:
        """Lease a slot to its holder. Caller holds ``_lock`` (every ``inflight += 1``).

        Returns the lease the holder must hand back on release. The soft cap is
        a leak guard only: a lease that never returns is a bug, and dropping the
        oldest entries degrades the estimate rather than growing memory.
        """
        self._next_lease += 1
        lease = self._next_lease
        self._holds[lease] = (priority, time.monotonic())
        while len(self._holds) > _HOLDS_SOFT_CAP:
            self._holds.pop(next(iter(self._holds)), None)
        return lease

    def _note_completion(self, lease: int | None, priority: bool, *, sample: bool = True) -> None:
        """Return a lease, recording its duration. Caller holds ``_lock``.

        ``lease=None`` is the fallback for a caller that took a slot through
        the bare :meth:`acquire`/:meth:`release` pair (tests, and anything that
        predates leases): the oldest hold in the same lane is returned instead,
        which is the best available guess and keeps the books balanced.

        ``sample=False`` for a slot that was cancelled during the dispatch
        race: it never ran upstream, and recording its ~0 s "duration" would
        teach the estimator that the lane is instant.
        """
        if lease is not None:
            held = self._holds.pop(lease, None)
            if held is None:
                # An unknown lease is a bug or a soft-cap eviction. Popping
                # "something else" would price a LIVE request as finished, so
                # take nothing and leave the estimate conservative.
                log(f"drain-lease-miss bid={self.bearer_id} lease={lease}")
                return
        else:
            oldest = min(
                (item for item in self._holds.items() if item[1][0] == priority),
                key=lambda item: item[1][1],
                default=None,
            )
            if oldest is None:
                return
            self._holds.pop(oldest[0], None)
            held = oldest[1]
        if sample:
            samples = self._priority_samples if held[0] else self._samples
            samples.append(max(0.0, time.monotonic() - held[1]))

    def _lane_holds(self, priority: bool) -> list[float]:
        """Dispatch stamps of the slots currently held in one lane."""
        return [started for lane, started in self._holds.values() if lane == priority]

    def _ahead_of(self, client_id: str | None, priority: bool) -> int:
        """Queued requests that will be dispatched BEFORE this client's arrival.

        The dispatcher rotates across clients, so an arrival does not queue
        behind the whole backlog — it queues behind at most one request per
        other active client per turn it has to take. For a client with ``own``
        requests already parked, its next one dispatches on turn ``own + 1``.
        A sibling AHEAD of it in the current rotation gets one more turn than a
        sibling behind it, so the two are counted differently: up to
        ``own + 1`` versus up to ``own``.

        Counting the whole depth instead would refuse a brand-new client stuck
        behind one chatty client's backlog — traffic the fair queue exists to
        serve promptly, and traffic this proxy serves today. ``client_id=None``
        (the published snapshot) assumes a NEW client, the common case for a
        consumer asking "can I be served".
        """
        queues = self._priority_queues if priority else self._queues
        order = self._priority_rr if priority else self._rr_order
        own = len(queues.get(client_id, ())) if client_id is not None else 0
        # A client not yet in the rotation joins at the tail, so every listed
        # client is "ahead" of it.
        position = order.index(client_id) if client_id in order else len(order)
        ahead = own
        for index, cid in enumerate(order):
            if cid == client_id:
                continue
            turns = own + 1 if index < position else own
            ahead += min(len(queues.get(cid, ())), turns)
        return ahead

    def drain_estimate(
        self,
        max_wait: float | None = None,
        *,
        priority: bool = False,
        client_id: str | None = None,
        now: float | None = None,
    ) -> DrainEstimate:
        """Whether one more arrival can reach a slot within ``max_wait``.

        ``max_wait=None`` reports against the CONFIGURED bound, which is what
        ``snapshot`` publishes; a caller holding a request passes its own
        effective (possibly inherited, therefore shorter) budget instead.

        The priority reserve is a DEDICATED pool, so a lane call is judged
        against ``PRIORITY_RESERVE_SLOTS`` and the lane's own queue — a full
        main pool must not reject the evaluator the reserve exists for.
        """
        priority = priority and config.PRIORITY_RESERVE_SLOTS > 0
        if priority:
            slots = config.PRIORITY_RESERVE_SLOTS
            busy = self.priority_inflight
            queued = self.priority_queued
            samples = self._priority_samples
        else:
            slots = self.max_concurrent
            busy = self.inflight - self.priority_inflight
            queued = self.queued_total
            samples = self._samples
        service_time_s, residuals, count, source = _service_estimate(
            samples, self._lane_holds(priority), now
        )
        return _compute_drain(
            slots=slots,
            busy=busy,
            queued=queued,
            ahead=self._ahead_of(client_id, priority),
            service_time_s=service_time_s,
            residuals=residuals,
            samples=count,
            source=source,
            evidenced=source != "cold",
            max_wait=config.QUEUE_MAX_WAIT_S if max_wait is None else max_wait,
        )

    def _recent_shrinks(self, now: float | None = None) -> int:
        """Count shrinks whose timestamp is inside ``2 * AIMD_BACKOFF_S``.

        The lookback intentionally extends past the cooldown gate by 2× so
        FAST recovery is *reachable in practice*. With a 1× window, ``grow``
        unblocks at exactly ``last_throttle_at + AIMD_BACKOFF_S``, which is
        the same instant the shrink timestamp ages out — recent collapses to
        0 and ``_effective_ramp_after`` always returns SLOW. Doubling the
        window opens an ``(AIMD_BACKOFF_S, 2 * AIMD_BACKOFF_S)`` post-cooldown
        band where FAST is observable. Storm detection still fires at
        ``STORM_THRESHOLD`` shrinks within the wider window — sustained
        pushback drops back to SLOW the moment the third shrink lands.

        Cheap O(maxlen) scan — typical len ≤ STORM_THRESHOLD; pathological
        storms cap at the deque's ``maxlen``. Snapshot/_may_grow callers may
        pass an explicit ``now`` to amortise the ``time.time()`` syscall.
        """
        if now is None:
            now = time.time()
        cutoff = now - 2 * config.AIMD_BACKOFF_S
        return sum(1 for ts in self._shrink_history if ts > cutoff)

    def _effective_ramp_after(self, now: float | None = None) -> int:
        """SLOW (default / storm) vs FAST (isolated recovery) ramp threshold.

        Three-state semantics — FAST is a *recovery* signal, not the default:

        - ``recent_shrinks == 0`` ⇒ SLOW. Clean state preserves the
          conservative pre-adaptive default and keeps backward compat with
          callers that pre-date PR #53 (e.g. ``test_clean_successes_grow``).
        - ``1 ≤ recent_shrinks < AIMD_STORM_THRESHOLD`` ⇒ FAST. One or two
          isolated 429s should not cost the full slow recovery — this is the
          whole point of the adaptive ramp.
        - ``recent_shrinks ≥ AIMD_STORM_THRESHOLD`` ⇒ SLOW. Sustained
          pushback; don't ramp aggressively or we will oscillate.

        Clamp invariant: ``effective ≤ AIMD_RAMP_AFTER``. If an operator
        accidentally sets ``AIMD_RAMP_AFTER_FAST > AIMD_RAMP_AFTER`` we
        silently honour the floor — FAST must never be slower than SLOW.
        """
        slow = config.AIMD_RAMP_AFTER
        recent = self._recent_shrinks(now)
        if recent == 0 or recent >= config.AIMD_STORM_THRESHOLD:
            return slow
        return min(config.AIMD_RAMP_AFTER_FAST, slow)

    def _may_grow(self) -> bool:
        """True when all four AIMD additive-increase guards currently hold.

        Caller must hold ``self._lock``. Guards (all required): enough
        consecutive successes since the last shrink (threshold is adaptive —
        SLOW under storm, FAST after an isolated transient); the backoff
        cooldown has elapsed; no open Retry-After window (we don't ramp while
        the server's explicit window is still open, even past the cooldown);
        and we are below the operator's hard ceiling.
        """
        now = time.time()
        return (
            self._successes_since_throttle >= self._effective_ramp_after(now)
            and now - self._last_throttle_at >= config.AIMD_BACKOFF_S
            and now >= self._retry_after_until
            and self.max_concurrent < self.hard_max
        )

    async def grow(self) -> int | None:
        """AIMD additive-increase. Called after every successful 2xx response.

        Ramps only when :meth:`_may_grow` holds (see its guards). Returns the
        new ceiling on bump, ``None`` otherwise. Always dispatches on bump so a
        queued request can grab the new slot immediately.
        """
        # PR #580: `observe` mode bumps counters without dispatching
        # (no queue exists). `off` skips entirely.
        if not self.observe_enabled:
            return None
        async with self._lock:
            self._successes_since_throttle += 1
            if not self._may_grow():
                return None
            self.max_concurrent += 1
            self._successes_since_throttle = 0
            if self.queue_enabled:
                self._try_dispatch()
            return self.max_concurrent

    def note_retry_after(self, seconds: float) -> float:
        """Record an upstream Retry-After (seconds) for this bearer.

        The next dispatch waits at least this long (:meth:`wait_retry_after`),
        and :meth:`grow` won't ramp until the window closes. Only extends the
        window, never shortens it. Honored uncapped — the Anthropic input
        bucket has been observed to return >120 s, so clamping would defeat the
        back-off.
        """
        if seconds <= 0:
            return self._retry_after_until
        if seconds > config.MAX_HOLD_RETRY_AFTER_S:
            self.require_retry_probe(block_while_retry=True)
        until = time.time() + seconds
        if until > self._retry_after_until:
            self._retry_after_until = until
            _persist_retry_after_until(self.bearer_id, until)
        self._last_throttle_at = max(self._last_throttle_at, time.time())
        return self._retry_after_until

    def retry_after_remaining(self) -> float:
        """Seconds left in the current Retry-After window, or 0 when clear."""
        return max(0.0, self._retry_after_until - time.time())

    def require_retry_probe(self, *, block_while_retry: bool = False) -> None:
        """Require single-flight message revalidation after this window clears."""
        require_retry_probe(self.bearer_id, block_while_retry=block_while_retry)

    def retry_probe_required(self) -> bool:
        return retry_probe_required(self.bearer_id)

    def retry_probe_inflight(self) -> bool:
        return retry_probe_inflight(self.bearer_id)

    def retry_probe_blocks_routing(self) -> bool:
        return retry_probe_blocks_routing(self.bearer_id)

    def try_begin_retry_probe(self) -> bool:
        if self.retry_after_remaining() > 0:
            return False
        return try_begin_retry_probe(self.bearer_id)

    async def wait_retry_probe(self) -> None:
        await wait_retry_probe(self.bearer_id)

    def finish_retry_probe(self, *, success: bool) -> bool:
        return finish_retry_probe(self.bearer_id, success=success)

    async def wait_retry_after(self) -> None:
        """Sleep until any outstanding Retry-After window has elapsed.

        Called just before dispatching to upstream so we honor the server's
        explicit back-off instead of spinning requests against a known-closed
        window. No-op when no Retry-After is pending.
        """
        wait = self.retry_after_remaining()
        if wait > 0:
            await asyncio.sleep(wait)

    async def acquire(self, client_id: str, *, priority: bool = False) -> bool:
        """Acquire one slot, returning only the effective lane.

        Back-compat shim over :meth:`acquire_lease` for callers that release
        without a lease (see ``_note_completion``).
        """
        effective, _lease = await self.acquire_lease(client_id, priority=priority)
        return effective

    async def acquire_lease(self, client_id: str, *, priority: bool = False) -> tuple[bool, int]:
        """Acquire one slot for ``client_id``, queueing fairly if necessary.

        In non-queue modes this just bumps ``inflight`` and returns. In queue
        mode it parks a future and awaits dispatch, cleaning up correctly if the
        caller is cancelled mid-wait. ``priority`` parks the future in the
        latency-lane (its own per-client RR queue, dispatched against the
        dedicated ``PRIORITY_RESERVE_SLOTS`` pool) so a short evaluator call
        never waits behind long generations holding every main slot.

        Returns the EFFECTIVE lane (a reserve of 0 disables the lane and
        demotes the call to normal traffic) and the slot's LEASE. Callers must
        pass both back to :meth:`release` so lane accounting and service-time
        bookkeeping stay symmetric.
        """
        priority = priority and config.PRIORITY_RESERVE_SLOTS > 0
        if not self.queue_enabled:
            async with self._lock:
                self.inflight += 1
                if priority:
                    self.priority_inflight += 1
                lease = self._note_dispatch(priority)
            return priority, lease

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        async with self._lock:
            if priority:
                q = self._priority_queues.setdefault(client_id, collections.deque())
                q.append(fut)
                if client_id not in self._priority_rr:
                    self._priority_rr.append(client_id)
            else:
                q = self._queues.setdefault(client_id, collections.deque())
                q.append(fut)
                if client_id not in self._rr_order:
                    self._rr_order.append(client_id)
            self._try_dispatch()
        try:
            # The dispatcher stamps the future's result with the lane that
            # actually granted the slot (True = priority pool) and the lease it
            # opened. This survives a mid-wait retune: reserve dropping to 0
            # migrates queued lane waiters into the normal queue, and whichever
            # loop dispatches is the one whose accounting the caller must undo.
            effective, lease = await fut
        except asyncio.CancelledError:
            await self._cancel_cleanup(client_id, fut)
            raise
        return effective, lease

    async def _cancel_cleanup(self, client_id: str, fut: asyncio.Future) -> None:
        """Undo a queued/dispatched slot when the caller is cancelled.

        Either removes the still-pending future from the client deque, or — if
        the slot was dispatched between ``set_result`` and the cancellation
        reaching us — releases the slot so it isn't leaked. The future's
        result carries the lane that dispatched it (True = priority pool), so
        the undo hits the same counter the dispatcher bumped even when a
        retune migrated the waiter between lanes while it was parked.
        """
        async with self._lock:
            removed = self._remove_pending(client_id, fut)
            if not removed and fut.done() and not fut.cancelled() and fut.exception() is None:
                effective, lease = fut.result()
                self.inflight -= 1
                if effective:
                    self.priority_inflight -= 1
                # Dispatched and cancelled in the same breath: return the lease
                # but do NOT sample it — it never reached upstream.
                self._note_completion(lease, bool(effective), sample=False)
                self._try_dispatch()

    def _remove_pending(self, client_id: str, fut: asyncio.Future) -> bool:
        """Remove ``fut`` from whichever queue holds it if still pending. Holds ``_lock``.

        Returns True if the future was found and removed (i.e. it had not been
        dispatched yet). Prunes empty deques + the round-robin entry.
        """
        if self._remove_from(self._priority_queues, self._priority_rr, client_id, fut):
            return True
        return self._remove_from(self._queues, self._rr_order, client_id, fut)

    @staticmethod
    def _remove_from(
        queues: dict[str, collections.deque[asyncio.Future]],
        order: collections.deque[str],
        client_id: str,
        fut: asyncio.Future,
    ) -> bool:
        """Remove ``fut`` from one queue family (main or priority) if pending."""
        q = queues.get(client_id)
        if q is None:
            return False
        removed = fut in q
        if removed:
            q.remove(fut)
        if not q:
            queues.pop(client_id, None)
            if client_id in order:
                order.remove(client_id)
        return removed

    async def release(self, *, priority: bool = False, lease: int | None = None) -> None:
        """Release one in-flight slot and dispatch the next queued request."""
        async with self._lock:
            self.inflight -= 1
            if priority:
                self.priority_inflight -= 1
            self._note_completion(lease, priority)
            self._try_dispatch()

    def _try_dispatch(self) -> None:
        """Wake queued futures. Caller must hold ``_lock``.

        The priority lane owns a DEDICATED pool: it dispatches while
        ``priority_inflight < PRIORITY_RESERVE_SLOTS``, regardless of the main
        pool. This survives the storm case that a shared ceiling does not:
        after an AIMD shrink, stale main inflight above the new ceiling would
        satisfy ``inflight >= max_concurrent + reserve`` and pinch a shared
        lane shut exactly when the evaluator needs it. Normal round-robin
        traffic is capped at ``max_concurrent`` main-pool slots
        (``inflight - priority_inflight``), so sustained priority load cannot
        starve it and the main pool cannot overrun the AIMD ceiling.
        Total upstream concurrency is bounded by
        ``max_concurrent + PRIORITY_RESERVE_SLOTS``.

        Dispatched futures are stamped with the lane that granted the slot
        (``set_result(True)`` = priority pool) so the awaiting ``acquire``
        returns the effective lane even if a retune moved the waiter while
        parked.
        """
        if config.PRIORITY_RESERVE_SLOTS <= 0 and self._priority_rr:
            # Reserve hot-tuned to 0 with lane waiters already parked: with the
            # lane closed nothing would ever dispatch them (Codex round-2 MAJOR
            # on PR #73) — migrate them into the normal RR structures. They
            # dispatch via the normal loop below, which stamps them demoted.
            self._migrate_priority_to_normal()
        while self.priority_inflight < config.PRIORITY_RESERVE_SLOTS and self._priority_rr:
            client_id = self._priority_rr.popleft()
            q = self._priority_queues.get(client_id)
            if not q:
                continue
            fut = q.popleft()
            if q:
                # Client has more queued — re-append at tail to keep rotation honest.
                self._priority_rr.append(client_id)
            else:
                self._priority_queues.pop(client_id, None)
            if fut.cancelled():
                continue
            self.inflight += 1
            self.priority_inflight += 1
            fut.set_result((True, self._note_dispatch(True)))
        while (self.inflight - self.priority_inflight) < self.max_concurrent and self._rr_order:
            client_id = self._rr_order.popleft()
            q = self._queues.get(client_id)
            if not q:
                continue
            fut = q.popleft()
            if q:
                # Client has more queued — re-append at tail to keep rotation honest.
                self._rr_order.append(client_id)
            else:
                self._queues.pop(client_id, None)
            if fut.cancelled():
                continue
            self.inflight += 1
            fut.set_result((False, self._note_dispatch(False)))

    def _migrate_priority_to_normal(self) -> None:
        """Move all parked lane waiters into the normal queues. Holds ``_lock``.

        Preserves per-client grouping: each client's lane deque is appended to
        its normal deque (arrival order within the client kept) and the client
        joins the normal rotation if not already in it.
        """
        for client_id in list(self._priority_rr):
            q = self._priority_queues.pop(client_id, None)
            if not q:
                continue
            self._queues.setdefault(client_id, collections.deque()).extend(q)
            if client_id not in self._rr_order:
                self._rr_order.append(client_id)
        self._priority_rr.clear()
        self._priority_queues.clear()

    @property
    def queued_total(self) -> int:
        """Requests parked in the fair queue, summed across clients.

        Exposed on its own because ``/__throttle/statusline`` needs the depth
        without allocating the per-client breakdown ``snapshot`` builds beside
        it. This still sums one deque per actively queued client (O(active queue
        clients)); the payload is O(1), but a maintained counter is the upgrade
        if measured queue-depth CPU becomes material.
        """
        return sum(len(q) for q in self._queues.values())

    @property
    def priority_queued(self) -> int:
        """Requests parked in the priority lane, summed across clients.

        The sibling of :attr:`queued_total` for the reserved lane. Routing's
        load score weighs both the same, and a caller that read only the fair
        queue would score a bearer whose reserve lane is backed up as idle.
        """
        return sum(len(q) for q in self._priority_queues.values())

    def snapshot(self) -> dict[str, object]:
        """Cheap dict snapshot for /__throttle/health."""
        now = time.time()
        recent = self._recent_shrinks(now)
        effective = self._effective_ramp_after(now)
        return {
            "inflight": self.inflight,
            "max_concurrent": self.max_concurrent,
            "hard_max": self.hard_max,
            "queue_mode": self.queue_mode,
            "queue_enabled": self.queue_enabled,
            "observe_enabled": self.observe_enabled,
            "last_throttle_at": self._last_throttle_at,
            "successes_since_throttle": self._successes_since_throttle,
            "retry_after_until": self._retry_after_until,
            "retry_probe_required": self.retry_probe_required(),
            "retry_probe_inflight": self.retry_probe_inflight(),
            "retry_probe_blocks_routing": self.retry_probe_blocks_routing(),
            "queued_total": self.queued_total,
            "priority_inflight": self.priority_inflight,
            "priority_queued": self.priority_queued,
            "queued_per_client": {cid: len(q) for cid, q in self._queues.items()},
            "rr_order": list(self._rr_order),
            # PR #53 adaptive ramp visibility — operator can read whether this
            # bearer is currently in storm mode + which ramp it will use next.
            # storm_mode is True ONLY when recent_shrinks crossed the threshold;
            # a fresh / clean limiter also returns SLOW from _effective_ramp_after
            # but is NOT a storm. Comparing effective == AIMD_RAMP_AFTER would
            # conflate the two states.
            "recent_shrinks": recent,
            "storm_mode": recent >= config.AIMD_STORM_THRESHOLD,
            "effective_ramp_after": effective,
            # Queue-DEPTH admission, against the CONFIGURED bound and a NEW
            # client — the same arithmetic the hot path runs, for the question a
            # consumer is actually asking. A request that inherits a SHORTER
            # end-to-end budget is judged against that shorter horizon instead,
            # so `max_wait_s` here says which horizon this bound describes.
            "drain": self.drain_estimate().as_dict(),
        }


class QueueWaitTimeout(Exception):
    """A request could not get a slot within its ``max_wait`` bound.

    Raised from ``_FairSlotContext.__aenter__`` BEFORE any slot is held (the
    ``asyncio.wait_for`` cancellation runs ``acquire``'s ``_cancel_cleanup``,
    which removes the parked future or releases a raced dispatch), so the
    caller never owes a ``release()`` and can answer the client with a clean
    503 + Retry-After while its transport is still alive.

    ``pre_queue`` distinguishes the two ways that happens: the request was
    parked and the bound expired, or the lane's own numbers put the wait past
    the budget before it was ever parked.
    """

    def __init__(
        self,
        max_wait: float,
        *,
        pre_queue: bool = False,
        estimate: DrainEstimate | None = None,
    ) -> None:
        super().__init__(f"no slot within {max_wait}s")
        self.max_wait = max_wait
        self.pre_queue = pre_queue
        self.estimate = estimate


class _FairSlotContext:
    """Async context manager returned by ``FairBearerLimiter.slot()``."""

    def __init__(
        self,
        limiter: FairBearerLimiter,
        client_id: str,
        priority: bool = False,
        max_wait: float | None = None,
    ) -> None:
        self.limiter = limiter
        self.client_id = client_id
        self.priority = priority
        self.max_wait = max_wait
        self.lease: int | None = None

    def _estimate(self) -> DrainEstimate:
        return self.limiter.drain_estimate(
            self.max_wait, priority=self.priority, client_id=self.client_id
        )

    async def __aenter__(self) -> _FairSlotContext:
        bounded = bool(self.max_wait) and self.limiter.queue_enabled
        if bounded:
            # Bounding the WAIT does not bound the DEPTH: a queue deeper than
            # this lane drains at its own measured service rate parks a request
            # whose estimated wait is already past the budget, burns the whole
            # patience window in silence, and answers too late to be useful
            # (01/09/2026 :8766 — 2 slots held 113.7 s / 221.2 s, 3 queued,
            # 30 s budget, 4 retries into the same wall). Refuse up front, with
            # the arithmetic attached, while the transport is wide open.
            #
            # The check and the enqueue below are one event-loop step: every
            # `_lock` critical section in this class is synchronous, so
            # `acquire`'s lock take never yields and a simultaneous burst is
            # serialized rather than all passing one stale estimate.
            estimate = self._estimate()
            if estimate.rejects:
                log(
                    f"queue-depth-reject bid={self.limiter.bearer_id} cid={self.client_id} "
                    f"slots={estimate.slots} busy={estimate.busy} queued={estimate.queued} "
                    f"ahead={estimate.ahead} service_s={estimate.service_time_s:g} "
                    f"source={estimate.source} est_wait_s={estimate.wait_s:g} "
                    f"max_wait_s={estimate.max_wait_s:g} max_depth={estimate.max_depth}"
                )
                raise QueueWaitTimeout(self.max_wait, pre_queue=True, estimate=estimate)
        # acquire_lease() echoes the EFFECTIVE lane (reserve 0 demotes to
        # normal) and the slot's lease; remember both so __aexit__ releases the
        # same pool and returns the same lease, even if the knob is retuned
        # mid-flight. Created only once the request is admitted, so a rejection
        # leaves no un-awaited coroutine behind.
        acquire = self.limiter.acquire_lease(self.client_id, priority=self.priority)
        if bounded:
            # wait_for cancels the parked acquire on timeout; its
            # CancelledError path (_cancel_cleanup) rolls the queue entry —
            # or a slot dispatched during the cancellation race — back, so
            # no release is owed here.
            try:
                self.priority, self.lease = await asyncio.wait_for(acquire, timeout=self.max_wait)
            except TimeoutError as exc:
                raise QueueWaitTimeout(self.max_wait, estimate=self._estimate()) from exc
        else:
            self.priority, self.lease = await acquire
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        await self.limiter.release(priority=self.priority, lease=self.lease)
        return False


async def _get_bearer_limiter(
    bid: str,
    queue_mode: str | None = None,
    max_concurrent: int | None = None,
) -> FairBearerLimiter:
    """Return the FairBearerLimiter for a bearer, allocating on first sight."""
    mode = queue_mode or config.QUEUE_MODE
    hard_max = max_concurrent or config.MAX_CONCURRENT
    lim = config.bearer_limiters.get(bid)
    if lim is not None:
        await _retune_limiter_hard_max(bid, lim, hard_max)
        if lim.queue_mode != mode:
            # Runtime target selection can promote an "off" local limiter to
            # "fair" when central is down. Do not downgrade an existing fair
            # limiter back to off on central recovery; queued futures would no
            # longer have a queue dispatcher to wake them.
            if not (mode == "off" and lim.queue_enabled):
                lim.set_queue_mode(mode)
                log(f"bearer-mode bid={bid} queue_mode={mode}")
        return lim
    async with bearer_limiter_lock:
        lim = config.bearer_limiters.get(bid)
        if lim is None:
            lim = FairBearerLimiter(hard_max, mode, bearer_id=bid)
            _restore_retry_after(lim, bid)
            config.bearer_limiters[bid] = lim
            config.bearer_state[bid] = {
                "inflight": 0,
                "queued": 0,
                "served": 0,
                # last_ratelimit: last-seen anthropic-ratelimit-* + retry-after.
                # unified: parsed OAuth unified-window utilization.
                "last_ratelimit": None,
                "unified": None,
                "clients": {},
            }
            M_AIMD_MAX.labels(bearer=bid).set(lim.max_concurrent)
            log(
                f"bearer-new bid={bid} max_concurrent={lim.max_concurrent} "
                f"hard_max={hard_max} queue_mode={mode}"
            )
        else:
            await _retune_limiter_hard_max(bid, lim, hard_max)
        if lim.queue_mode != mode and not (mode == "off" and lim.queue_enabled):
            lim.set_queue_mode(mode)
            log(f"bearer-mode bid={bid} queue_mode={mode}")
        return lim
