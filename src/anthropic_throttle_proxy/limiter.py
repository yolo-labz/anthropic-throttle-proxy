"""Per-bearer concurrency limiter with weighted-fair-queueing + AIMD ceiling.

Replaces a plain ``asyncio.Semaphore``. Same in-flight cap, but queued
requests are dispatched round-robin across distinct ``client_id``s so no
client can monopolize slots under sustained backlog, and the live ceiling
shrinks/grows reactively (AIMD) on upstream rate pushback.
"""

from __future__ import annotations

import asyncio
import collections
import time

from . import config
from .config import log
from .metrics import M_AIMD_MAX

# Late-bound in main() (needs the running loop) — guards the registry below.
bearer_limiter_lock: asyncio.Lock | None = None


def set_lock(lock: asyncio.Lock) -> None:
    """Bind the registry lock (called once from ``main()`` on the running loop)."""
    global bearer_limiter_lock
    bearer_limiter_lock = lock


def _initial_live_cap(hard_max: int) -> int:
    """Initial AIMD live cap for a new bearer, bounded by the hard ceiling."""
    return min(hard_max, max(config.AIMD_MIN, config.AIMD_INITIAL_CONCURRENT))


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

    def __init__(self, max_concurrent: int, queue_mode: str) -> None:
        # PR #575/PR #40: `hard_max` is the operator-set upper bound (e.g. 32).
        # `max_concurrent` is the LIVE ceiling that starts conservatively, grows
        # after clean traffic, and shrinks on upstream 429/503.
        self.hard_max = max_concurrent
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
        self._lock = asyncio.Lock()
        # _last_throttle_at: monotonic-ish wall clock of the last shrink.
        # _successes_since_throttle: consecutive 2xx since that shrink.
        # _retry_after_until: wall-clock end of any open Retry-After window.
        self._last_throttle_at = 0.0
        self._successes_since_throttle = 0
        self._retry_after_until = 0.0

    def set_queue_mode(self, queue_mode: str) -> None:
        """Switch the limiter's admission mode for future acquires.

        Used by the local tier when central fallback becomes direct-upstream:
        a desktop configured as pass-through while central is healthy must
        still enforce the local fair queue when central is down.
        """
        self.queue_mode = queue_mode
        self.queue_enabled = queue_mode in {"fair", "reactive"}
        self.observe_enabled = queue_mode != "off"

    def slot(self, client_id: str) -> _FairSlotContext:
        """Return an async context manager that holds one slot for ``client_id``."""
        return _FairSlotContext(self, client_id)

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
            return new_max

    def _may_grow(self) -> bool:
        """True when all four AIMD additive-increase guards currently hold.

        Caller must hold ``self._lock``. Guards (all required): enough
        consecutive successes since the last shrink; the backoff cooldown has
        elapsed; no open Retry-After window (we don't ramp while the server's
        explicit window is still open, even past the cooldown); and we are
        below the operator's hard ceiling.
        """
        now = time.time()
        return (
            self._successes_since_throttle >= config.AIMD_RAMP_AFTER
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

    async def acquire(self, client_id: str) -> None:
        """Acquire one slot for ``client_id``, queueing fairly if necessary.

        In non-queue modes this just bumps ``inflight`` and returns. In queue
        mode it parks a future in the client's deque and awaits dispatch,
        cleaning up correctly if the caller is cancelled mid-wait.
        """
        if not self.queue_enabled:
            async with self._lock:
                self.inflight += 1
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        async with self._lock:
            q = self._queues.setdefault(client_id, collections.deque())
            q.append(fut)
            if client_id not in self._rr_order:
                self._rr_order.append(client_id)
            self._try_dispatch()
        try:
            await fut
        except asyncio.CancelledError:
            await self._cancel_cleanup(client_id, fut)
            raise

    async def _cancel_cleanup(self, client_id: str, fut: asyncio.Future) -> None:
        """Undo a queued/dispatched slot when the caller is cancelled.

        Either removes the still-pending future from the client deque, or — if
        the slot was dispatched between ``set_result`` and the cancellation
        reaching us — releases the slot so it isn't leaked.
        """
        async with self._lock:
            removed = self._remove_pending(client_id, fut)
            if not removed and fut.done() and not fut.cancelled() and fut.exception() is None:
                self.inflight -= 1
                self._try_dispatch()

    def _remove_pending(self, client_id: str, fut: asyncio.Future) -> bool:
        """Remove ``fut`` from the client deque if still queued. Holds ``_lock``.

        Returns True if the future was found and removed (i.e. it had not been
        dispatched yet). Prunes empty deques + the round-robin entry.
        """
        q = self._queues.get(client_id)
        if q is None:
            return False
        removed = False
        try:
            q.remove(fut)
            removed = True
        except ValueError:
            pass
        if not q:
            self._queues.pop(client_id, None)
            try:
                self._rr_order.remove(client_id)
            except ValueError:
                pass
        return removed

    async def release(self) -> None:
        """Release one in-flight slot and dispatch the next queued request."""
        async with self._lock:
            self.inflight -= 1
            self._try_dispatch()

    def _try_dispatch(self) -> None:
        """Wake queued futures up to the live ceiling. Caller must hold ``_lock``."""
        while self.inflight < self.max_concurrent and self._rr_order:
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
            fut.set_result(None)

    def snapshot(self) -> dict[str, object]:
        """Cheap dict snapshot for /__throttle/health."""
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
            "queued_per_client": {cid: len(q) for cid, q in self._queues.items()},
            "rr_order": list(self._rr_order),
        }


class _FairSlotContext:
    """Async context manager returned by ``FairBearerLimiter.slot()``."""

    def __init__(self, limiter: FairBearerLimiter, client_id: str) -> None:
        self.limiter = limiter
        self.client_id = client_id

    async def __aenter__(self) -> _FairSlotContext:
        await self.limiter.acquire(self.client_id)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        await self.limiter.release()
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
            lim = FairBearerLimiter(hard_max, mode)
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
        if lim.queue_mode != mode:
            if not (mode == "off" and lim.queue_enabled):
                lim.set_queue_mode(mode)
                log(f"bearer-mode bid={bid} queue_mode={mode}")
        return lim
