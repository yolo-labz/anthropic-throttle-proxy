"""Standalone T003 foundation; no production wiring or implicit configuration.

One loop owns tickets; one worker owns ALL pools and filesystem operations.
Callers guarantee exclusive state-file custody, a trustworthy clock and that
handoff() immediately precedes transport with no intervening await. Only the
reserve context may refund an unsent ticket. Dispatched debt is always retained.

ponytail: bound submissions, not filesystem execution. Python cannot interrupt
fsync safely. A timed-out close is still draining; keep the loop alive and never
transfer file custody until aclose finishes. No thread/process locking service,
provider budgets, scheduler, response accounting or settlement is added here.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

from .ledger import WINDOW_S, Budgets, Lease, LedgerPool, _pos_int, _valid_key, _valid_time


class OwnerUnavailable(RuntimeError):
    """Not started, closing, full, or previously faulted: no dispatch allowed."""


class OwnerFaulted(OwnerUnavailable):
    """A worker operation failed; no later operation may spend provisional state."""


class BudgetRefused(RuntimeError):
    """No lease was issued; this is not provider pushback."""


@dataclass(frozen=True)
class Scope:
    """Explicit canonical scope and exclusively caller-owned persistent state."""

    key: tuple[str, str, str]
    budgets: Budgets
    state_path: str
    allow_cold_start: bool

    def __post_init__(self) -> None:
        if not isinstance(self.key, tuple) or not _valid_key(self.key):
            raise ValueError("scope must be a canonical three-string tuple")
        if not isinstance(self.budgets, Budgets):
            raise ValueError("explicit Budgets required")
        if not isinstance(self.state_path, str) or not os.path.isabs(self.state_path):
            raise ValueError("an absolute caller-owned state path is required")
        if type(self.allow_cold_start) is not bool:
            raise ValueError("cold-start permission must be explicit boolean")


class _Reservation:
    """Loop-owned, context-bound handoff marker, not a transport receipt."""

    def __init__(self, owner: PersistenceOwner, lease: Lease) -> None:
        self._owner = owner
        self._lease = lease
        self._active = True
        self._sent = False

    def handoff(self) -> Lease:
        """Mark possibly-spent BEFORE transport. Never await between the two."""
        self._owner._available()
        if not self._active or self._sent:
            raise ValueError("reservation already handed off or context exited")
        now = self._owner._clock()
        if not _valid_time(now) or not 0 <= now - self._lease.at < WINDOW_S:
            raise OwnerUnavailable("reservation expired or clock moved backwards")
        self._sent = True
        self._owner._release()
        return self._lease


class PersistenceOwner:
    """Explicitly started single-worker owner, bound to the constructing loop.

    max_pending bounds operations PLUS yielded unsent tickets, reserving room
    for their cleanup. Excess work refuses immediately. wait_timeout bounds
    each public wait, not an fsync or caller code inside a reservation context.
    The injected clock must be thread-safe and nonblocking (also read at handoff).
    """

    def __init__(
        self,
        scopes: Sequence[Scope],
        *,
        max_pending: int,
        wait_timeout: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        scopes = tuple(scopes)
        if not scopes or any(not isinstance(scope, Scope) for scope in scopes):
            raise ValueError("explicit nonempty scopes required")
        self._scopes = {scope.key: scope for scope in scopes}
        if len(self._scopes) != len(scopes):
            raise ValueError("duplicate canonical scope")
        if not _pos_int(max_pending) or not _valid_time(wait_timeout) or wait_timeout == 0:
            raise ValueError("positive pending bound and finite positive wait required")
        self._loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prospective-owner")
        self._clock = clock
        self._max_pending = max_pending
        self._wait_timeout = wait_timeout
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._fault = threading.Event()
        self._ready = False
        self._closing = False
        self._start_future: asyncio.Future | None = None
        self._close_task: asyncio.Task | None = None
        self._cleanups: set[asyncio.Task] = set()
        self._pools: dict[tuple[str, str, str], LedgerPool] = {}  # worker-only

    @property
    def pending(self) -> int:
        """Loop-side work/unsent count, including cleanup abandoned by callers."""
        return self._pending

    @property
    def faulted(self) -> bool:
        return self._fault.is_set()

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise OwnerUnavailable("owner belongs to another event loop")

    def _available(self, *, require_ready: bool = True) -> None:
        self._check_loop()
        if self.faulted:
            raise OwnerFaulted("persistence owner faulted; restore only after drain")
        if self._closing or (require_ready and not self._ready):
            raise OwnerUnavailable("persistence owner is not open")

    def _claim(self, *, require_ready: bool = True) -> None:
        self._available(require_ready=require_ready)
        if self._pending >= self._max_pending:
            raise OwnerUnavailable("persistence work bound reached")
        self._pending += 1
        self._idle.clear()

    def _release(self) -> None:
        self._pending -= 1
        if self._pending == 0:
            self._idle.set()

    def _run(self, action: Callable):
        """Worker-only fault barrier, checked before EVERY queued operation."""
        if self.faulted:
            raise OwnerFaulted("persistence owner faulted")
        try:
            return action()
        except Exception:
            self._fault.set()
            raise

    def _submit(self, action: Callable, *args) -> asyncio.Future:
        # Caller already holds a permit, including for mandatory rollback.
        try:
            return self._loop.run_in_executor(self._executor, self._run, partial(action, *args))
        except Exception as exc:
            self._fault.set()
            failed = self._loop.create_future()
            failed.set_exception(exc)
            return failed  # normal completion/cleanup still releases the permit

    async def _wait(self, future):
        return await asyncio.wait_for(asyncio.shield(future), self._wait_timeout)

    def _operation_done(self, future: asyncio.Future) -> None:
        future.exception()  # observe late failures even when the waiter left
        self._release()

    def _load(self) -> None:
        identities: set[object] = set()
        for scope in self._scopes.values():
            path = os.path.realpath(scope.state_path)
            try:
                stat = os.stat(path)
                identity = (stat.st_dev, stat.st_ino)
            except FileNotFoundError:
                identity = path
            if identity in identities:
                raise ValueError("scope state files must be distinct")
            identities.add(identity)
            # Bypass LedgerPool's permissive missing-file startup: custody is
            # explicit here, including permission for an actual cold start.
            pool = LedgerPool(scope.budgets, clock=self._clock)
            pool.state_path = path
            try:
                pool._load()
            except FileNotFoundError:
                if not scope.allow_cold_start:
                    raise
            if set(pool.lanes) - {scope.key}:
                raise ValueError("state file contains a different scope")
            self._pools[scope.key] = pool

    async def start(self) -> None:
        """Load on the worker. A cancelled/timed-out start can be awaited again."""
        self._available(require_ready=False)
        if self._start_future is None:
            self._claim(require_ready=False)
            self._start_future = self._submit(self._load)
            self._start_future.add_done_callback(self._operation_done)
        await self._wait(self._start_future)
        self._available(require_ready=False)
        self._ready = True

    def _known_scope(self, key: tuple[str, str, str]) -> None:
        if not isinstance(key, tuple) or not _valid_key(key) or key not in self._scopes:
            raise ValueError("unknown canonical scope")

    def _debit(self, key, input_tokens: int, output_bound: int) -> Lease | None:
        return self._pools[key].check_and_debit(key, input_tokens, output_bound)

    def _cancel(self, lease: Lease) -> None:
        pool = self._pools[lease.key]
        pool.ledger_for(lease.key).cancel_unsent(lease)
        pool.save()  # failure faults _run before ANY later admission can spend

    async def _rollback(self, future: asyncio.Future) -> None:
        try:
            try:
                lease = await asyncio.shield(future)
            except Exception:
                return  # no acknowledged lease; _run already faulted the owner
            if lease is not None:
                await asyncio.shield(self._submit(self._cancel, lease))
        finally:
            self._release()

    def _cleanup_done(self, task: asyncio.Task) -> None:
        self._cleanups.discard(task)
        task.exception()  # fault remains observable through faulted/aclose

    def _rollback_later(self, future: asyncio.Future) -> asyncio.Task:
        task = self._loop.create_task(self._rollback(future))
        self._cleanups.add(task)
        task.add_done_callback(self._cleanup_done)
        return task

    def _finish(self, ticket: _Reservation, future: asyncio.Future) -> asyncio.Task | None:
        ticket._active = False
        return None if ticket._sent else self._rollback_later(future)

    @asynccontextmanager
    async def reserve(
        self, key: tuple[str, str, str], input_tokens: int, output_bound: int
    ) -> AsyncIterator[_Reservation]:
        """Yield only persisted debt; unsent exits roll back on the same worker.

        Acquisition timeout/cancellation keeps cleanup owned without delaying
        the original exception. Normal unsent exit waits for durable rollback;
        cancelling/timing out that wait also leaves the cleanup tracked.
        """
        self._known_scope(key)
        if not _pos_int(input_tokens) or not _pos_int(output_bound):
            raise ValueError("positive exact integer token bounds required")
        self._claim()
        future = self._submit(self._debit, key, input_tokens, output_bound)
        try:
            lease = await self._wait(future)
        except BaseException:
            self._rollback_later(future)
            raise
        if lease is None:
            self._release()
            raise BudgetRefused("reservation exceeds configured budget")
        ticket = _Reservation(self, lease)
        try:
            yield ticket
        except BaseException:
            self._finish(ticket, future)
            raise
        else:
            cleanup = self._finish(ticket, future)
            if cleanup is not None:
                await self._wait(cleanup)

    def _snapshot(self, key) -> dict[str, int]:
        return self._pools[key].ledger_for(key).snapshot()

    async def snapshot(self, key: tuple[str, str, str]) -> dict[str, int]:
        """Copied counters; even expiry pruning stays on the owning worker."""
        self._known_scope(key)
        self._claim()
        future = self._submit(self._snapshot, key)
        future.add_done_callback(self._operation_done)
        return await self._wait(future)

    async def _drain(self) -> None:
        await self._idle.wait()
        self._executor.shutdown(wait=False, cancel_futures=True)  # no loop-side join

    async def aclose(self) -> None:
        """Stop admission, drain tracked work, and surface any worker fault.

        Timeout/cancellation leaves the same close task running. Retry aclose,
        not owner construction, before transferring exclusive file custody.
        """
        self._check_loop()
        self._closing = True
        if self._close_task is None:
            self._close_task = self._loop.create_task(self._drain())
        await self._wait(self._close_task)
        if self.faulted:
            raise OwnerFaulted("persistence owner drained with a fault")
