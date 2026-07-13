"""Per-bearer concurrency limiter with weighted-fair-queueing + AIMD ceiling.

Replaces a plain ``asyncio.Semaphore``. Same in-flight cap, but queued
requests are dispatched round-robin across distinct ``client_id``s so no
client can monopolize slots under sustained backlog, and the live ceiling
shrinks/grows reactively (AIMD) on upstream rate pushback.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import time
from pathlib import Path

from . import config
from .config import log
from .metrics import M_AIMD_MAX

# Late-bound in main() (needs the running loop) — guards the registry below.
bearer_limiter_lock: asyncio.Lock | None = None
_retry_after_state: dict[str, float] | None = None


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
    _retry_after_state = {
        str(bid): float(until)
        for bid, until in raw.items()
        if isinstance(until, (int, float)) and float(until) > now
    }
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
    lim._last_throttle_at = max(lim._last_throttle_at, time.time())
    log(f"bearer-retry-after-restore bid={bid} remaining={int(until - time.time())}s")


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
        self._lock = asyncio.Lock()
        # _last_throttle_at: monotonic-ish wall clock of the last shrink.
        # _successes_since_throttle: consecutive 2xx since that shrink.
        # _retry_after_until: wall-clock end of any open Retry-After window.
        self._last_throttle_at = 0.0
        self._successes_since_throttle = 0
        self._retry_after_until = 0.0
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
        until = time.time() + seconds
        if until > self._retry_after_until:
            self._retry_after_until = until
            _persist_retry_after_until(self.bearer_id, until)
        self._last_throttle_at = max(self._last_throttle_at, time.time())
        return self._retry_after_until

    def retry_after_remaining(self) -> float:
        """Seconds left in the current Retry-After window, or 0 when clear."""
        return max(0.0, self._retry_after_until - time.time())

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
        """Acquire one slot for ``client_id``, queueing fairly if necessary.

        In non-queue modes this just bumps ``inflight`` and returns. In queue
        mode it parks a future and awaits dispatch, cleaning up correctly if the
        caller is cancelled mid-wait. ``priority`` parks the future in the
        latency-lane (its own per-client RR queue, dispatched against the
        dedicated ``PRIORITY_RESERVE_SLOTS`` pool) so a short evaluator call
        never waits behind long generations holding every main slot.

        Returns the EFFECTIVE lane (a reserve of 0 disables the lane and
        demotes the call to normal traffic). Callers that later invoke
        :meth:`release` directly must pass this value back as ``priority`` so
        the lane accounting stays symmetric.
        """
        priority = priority and config.PRIORITY_RESERVE_SLOTS > 0
        if not self.queue_enabled:
            async with self._lock:
                self.inflight += 1
                if priority:
                    self.priority_inflight += 1
            return priority

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
            # actually granted the slot (True = priority pool). This survives
            # a mid-wait retune: reserve dropping to 0 migrates queued lane
            # waiters into the normal queue, and whichever loop dispatches is
            # the one whose accounting the caller must undo on release.
            effective: bool = await fut
        except asyncio.CancelledError:
            await self._cancel_cleanup(client_id, fut)
            raise
        return effective

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
                self.inflight -= 1
                if fut.result():
                    self.priority_inflight -= 1
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

    async def release(self, *, priority: bool = False) -> None:
        """Release one in-flight slot and dispatch the next queued request."""
        async with self._lock:
            self.inflight -= 1
            if priority:
                self.priority_inflight -= 1
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
            fut.set_result(True)
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
            fut.set_result(False)

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
            "queued_total": sum(len(q) for q in self._queues.values()),
            "priority_inflight": self.priority_inflight,
            "priority_queued": sum(len(q) for q in self._priority_queues.values()),
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
        }


class QueueWaitTimeout(Exception):
    """A queued request exceeded its ``max_wait`` bound without getting a slot.

    Raised from ``_FairSlotContext.__aenter__`` BEFORE any slot is held (the
    ``asyncio.wait_for`` cancellation runs ``acquire``'s ``_cancel_cleanup``,
    which removes the parked future or releases a raced dispatch), so the
    caller never owes a ``release()`` and can answer the client with a clean
    503 + Retry-After while its transport is still alive.
    """

    def __init__(self, max_wait: float) -> None:
        super().__init__(f"no slot within {max_wait}s")
        self.max_wait = max_wait


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

    async def __aenter__(self) -> _FairSlotContext:
        # acquire() echoes the EFFECTIVE lane (reserve 0 demotes to normal);
        # remember it so __aexit__ releases the same pool it acquired from,
        # even if the knob is retuned mid-flight.
        acquire = self.limiter.acquire(self.client_id, priority=self.priority)
        if self.max_wait and self.limiter.queue_enabled:
            # wait_for cancels the parked acquire on timeout; its
            # CancelledError path (_cancel_cleanup) rolls the queue entry —
            # or a slot dispatched during the cancellation race — back, so
            # no release is owed here.
            try:
                self.priority = await asyncio.wait_for(acquire, timeout=self.max_wait)
            except TimeoutError as exc:
                raise QueueWaitTimeout(self.max_wait) from exc
        else:
            self.priority = await acquire
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        await self.limiter.release(priority=self.priority)
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
