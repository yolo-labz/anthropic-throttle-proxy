"""Offline T003 owner falsifiers. Fictional budgets; no transport or activation.

Thread gates have an emergency timeout solely to avoid wedging a broken test.
The event loop must run while the real ledger save is stopped inside fsync.
"""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from anthropic_throttle_proxy import ledger as ledger_module
from anthropic_throttle_proxy.ledger import Budgets, LaneLedger, LedgerPool
from anthropic_throttle_proxy.prospective_admission import (
    BudgetRefused,
    OwnerFaulted,
    OwnerUnavailable,
    PersistenceOwner,
    Scope,
)

KEY = ("fixture-upstream", "account-one", "fixture-model")
OTHER = (KEY[0], "account-two", KEY[2])


def scope_at(tmp_path, *, key=KEY, name="ledger.json", cold=True):
    return Scope(key, Budgets(2, 100), str(tmp_path / name), allow_cold_start=cold)


@pytest.fixture
async def owners(tmp_path):
    created = []

    async def make(*, scopes=None, max_pending=4, wait_timeout=2.0, clock=lambda: 1000.0):
        owner = PersistenceOwner(
            scopes if scopes is not None else [scope_at(tmp_path)],
            max_pending=max_pending,
            wait_timeout=wait_timeout,
            clock=clock,
        )
        created.append(owner)
        await owner.start()
        return owner

    yield make
    for owner in created:
        try:
            await owner.aclose()
        except OwnerFaulted:
            assert owner.faulted  # still drain every worker and verify expected state


class FsyncGate:
    def __init__(self, monkeypatch, *, call=1, fail=False):
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.thread = None
        self.calls = 0
        loop = asyncio.get_running_loop()
        fsync = ledger_module.os.fsync

        def blocked(fd):
            self.calls += 1
            if self.calls == call:
                self.thread = threading.get_ident()
                loop.call_soon_threadsafe(self.entered.set)
                if not self.release.wait(5):
                    raise TimeoutError("test did not release fsync gate")
                if fail:
                    raise OSError("injected fsync failure")
            fsync(fd)

        monkeypatch.setattr(ledger_module.os, "fsync", blocked)

    async def wait(self):
        await asyncio.wait_for(self.entered.wait(), 2)


async def dispatch(owner, *, key=KEY, sent=None, amount=50):
    async with owner.reserve(key, amount, amount) as ticket:
        lease = ticket.handoff()  # fixture handoff, NEVER actual network
        if sent is not None:
            sent.append(lease)
        return lease


def disk_counts(tmp_path):
    entries = json.loads((tmp_path / "ledger.json").read_text())["lanes"][0]["entries"]
    return len(entries), sum(entry["tokens"] for entry in entries)


async def test_blocked_fsync_keeps_loop_alive_and_does_not_acknowledge(owners, monkeypatch):
    owner = await owners(max_pending=1)
    gate = FsyncGate(monkeypatch)
    sent = []
    task = asyncio.create_task(dispatch(owner, sent=sent))
    try:
        await gate.wait()
        ticks = []
        for tick in range(5):
            await asyncio.sleep(0)
            ticks.append(tick)
        assert ticks == list(range(5))
        assert gate.thread != threading.get_ident()
        assert not task.done() and sent == [] and owner.pending == 1
    finally:
        gate.release.set()
    lease = await task
    assert sent == [lease] and owner.pending == 0


async def test_bound_includes_running_queued_and_abandoned_cleanup(owners, monkeypatch):
    owner = await owners(max_pending=2)
    gate = FsyncGate(monkeypatch)
    first = asyncio.create_task(dispatch(owner))
    queued = None
    try:
        await gate.wait()
        queued = asyncio.create_task(owner.snapshot(KEY))
        await asyncio.sleep(0)
        assert owner.pending == 2
        with pytest.raises(OwnerUnavailable, match="bound"):
            await dispatch(owner)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert owner.pending == 2  # abandoned debit still owns cleanup capacity
        with pytest.raises(OwnerUnavailable, match="bound"):
            await owner.snapshot(KEY)
    finally:
        gate.release.set()
        tasks = [first] if queued is None else [first, queued]
        await asyncio.gather(*tasks, return_exceptions=True)
    await owner.aclose()
    restored = await owners()
    assert await restored.snapshot(KEY) == {"requests": 0, "tokens": 0}


@pytest.mark.parametrize("abandon", ["cancel", "timeout"])
async def test_acquisition_abandonment_rolls_back_after_worker_finishes(
    owners, monkeypatch, abandon
):
    owner = await owners(max_pending=1, wait_timeout=0.1)
    gate = FsyncGate(monkeypatch)
    sent = []
    task = asyncio.create_task(dispatch(owner, sent=sent))
    try:
        await gate.wait()
        if abandon == "cancel":
            task.cancel()
        expected = asyncio.CancelledError if abandon == "cancel" else TimeoutError
        with pytest.raises(expected):
            await task
        assert owner.pending == 1 and sent == []
        with pytest.raises(OwnerUnavailable, match="bound"):
            await dispatch(owner)
    finally:
        gate.release.set()
    await owner.aclose()
    restored = await owners()
    assert await restored.snapshot(KEY) == {"requests": 0, "tokens": 0}
    assert (await dispatch(restored)).seq == 2


@pytest.mark.parametrize("handoff", [False, True])
async def test_context_cancellation_before_and_after_handoff(owners, handoff):
    owner = await owners(max_pending=1)
    ready = asyncio.Event()
    never = asyncio.Event()

    async def request():
        async with owner.reserve(KEY, 50, 50) as ticket:
            if handoff:
                ticket.handoff()
            ready.set()
            await never.wait()

    task = asyncio.create_task(request())
    await asyncio.wait_for(ready.wait(), 2)
    assert owner.pending == (0 if handoff else 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await owner.aclose()
    restored = await owners()
    expected = {"requests": 1, "tokens": 100} if handoff else {"requests": 0, "tokens": 0}
    assert await restored.snapshot(KEY) == expected
    if handoff:
        with pytest.raises(BudgetRefused):
            await dispatch(restored)


async def test_normal_unsent_exit_refunds_and_handoff_is_one_shot(owners):
    owner = await owners(max_pending=1)
    async with owner.reserve(KEY, 50, 50) as unsent:
        with pytest.raises(OwnerUnavailable, match="bound"):
            await owner.snapshot(KEY)
    assert await owner.snapshot(KEY) == {"requests": 0, "tokens": 0}
    with pytest.raises(ValueError, match="exited"):
        unsent.handoff()
    async with owner.reserve(KEY, 50, 50) as ticket:
        lease = ticket.handoff()
        assert lease.seq == 2
        with pytest.raises(ValueError, match="handed off"):
            ticket.handoff()
        assert await owner.snapshot(KEY) == {"requests": 1, "tokens": 100}
    assert await owner.snapshot(KEY) == {"requests": 1, "tokens": 100}


@pytest.mark.parametrize("now", [999.0, 1060.0, float("nan")])
async def test_bad_clock_or_expired_ticket_never_hands_off(owners, now):
    clock = [1000.0]
    owner = await owners(clock=lambda: clock[0])
    async with owner.reserve(KEY, 50, 50) as ticket:
        clock[0] = now
        with pytest.raises(OwnerUnavailable, match="expired"):
            ticket.handoff()
        clock[0] = 1000.0  # restore clock before rollback's own validation
    assert await owner.snapshot(KEY) == {"requests": 0, "tokens": 0}


async def test_restart_retains_debt_and_scope_budgets_are_independent(owners, tmp_path):
    other = replace(scope_at(tmp_path, key=OTHER, name="other.json"), budgets=Budgets(2, 200))
    scopes = [scope_at(tmp_path), other]
    owner = await owners(scopes=scopes)
    await dispatch(owner)
    await dispatch(owner, key=OTHER)
    await dispatch(owner, key=OTHER)
    await owner.aclose()
    restored = await owners(scopes=[replace(scope, allow_cold_start=False) for scope in scopes])
    for key, count in ((KEY, 1), (OTHER, 2)):
        assert await restored.snapshot(key) == {"requests": count, "tokens": count * 100}
        with pytest.raises(BudgetRefused):
            await dispatch(restored, key=key)


def record_thread(original, calls):
    def wrapped(*args, **kwargs):
        calls.append((original.__name__, threading.get_ident()))
        return original(*args, **kwargs)

    return wrapped


async def test_all_pool_operations_share_one_non_loop_thread(owners, tmp_path, monkeypatch):
    calls = []
    for cls, name in (
        (LedgerPool, "__post_init__"),
        (LedgerPool, "_load"),
        (LedgerPool, "save"),
        (LedgerPool, "check_and_debit"),
        (LaneLedger, "cancel_unsent"),
        (LaneLedger, "snapshot"),
    ):
        monkeypatch.setattr(cls, name, record_thread(getattr(cls, name), calls))
    owner = await owners(
        scopes=[scope_at(tmp_path), scope_at(tmp_path, key=OTHER, name="other.json")]
    )
    async with owner.reserve(KEY, 50, 50):
        pass
    await dispatch(owner, key=OTHER)
    await owner.snapshot(KEY)
    assert {name for name, _thread in calls} == {
        "__post_init__",
        "_load",
        "save",
        "check_and_debit",
        "cancel_unsent",
        "snapshot",
    }
    threads = {thread for _name, thread in calls}
    assert len(threads) == 1 and threading.get_ident() not in threads


def break_persistence(monkeypatch, failure):
    fsync = ledger_module.os.fsync
    calls = 0

    def broken_replace(*_args):
        raise OSError("injected replace failure")

    def broken_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == (2 if failure == "directory-fsync" else 1):
            raise OSError("injected fsync failure")
        fsync(fd)

    if failure == "replace":
        monkeypatch.setattr(ledger_module.os, "replace", broken_replace)
    else:
        monkeypatch.setattr(ledger_module.os, "fsync", broken_fsync)


@pytest.mark.parametrize("failure", ["file-fsync", "replace", "directory-fsync"])
@pytest.mark.parametrize("phase", ["debit", "rollback"])
async def test_save_failure_faults_later_admission(owners, tmp_path, monkeypatch, failure, phase):
    owner = await owners()
    yielded = []
    if phase == "debit":
        break_persistence(monkeypatch, failure)
    with pytest.raises(OSError):
        async with owner.reserve(KEY, 50, 50) as ticket:
            yielded.append(ticket)
            if phase == "rollback":
                break_persistence(monkeypatch, failure)
    assert bool(yielded) == (phase == "rollback")
    assert owner.faulted
    with pytest.raises(OwnerFaulted):
        await dispatch(owner)
    with pytest.raises(OwnerFaulted):
        await owner.snapshot(KEY)
    with pytest.raises(OwnerFaulted):
        await owner.aclose()
    if phase == "rollback":
        # Before replace: disk debt remains. After replace: removal can already
        # be visible, but no in-process provisional refund may be spent.
        assert disk_counts(tmp_path) == ((0, 0) if failure == "directory-fsync" else (1, 100))


async def test_failed_rollback_blocks_already_queued_admission(owners, monkeypatch, tmp_path):
    owner = await owners(max_pending=2)
    gate = FsyncGate(monkeypatch, call=3, fail=True)  # debit's two fsyncs precede rollback

    async def unsent():
        async with owner.reserve(KEY, 50, 50):
            pass

    first = asyncio.create_task(unsent())
    second = None
    sent = []
    try:
        await gate.wait()
        second = asyncio.create_task(dispatch(owner, sent=sent))
        await asyncio.sleep(0)
        assert owner.pending == 2
    finally:
        gate.release.set()
    with pytest.raises(OSError):
        await first
    with pytest.raises(OwnerFaulted):
        await second
    assert sent == [] and owner.faulted
    with pytest.raises(OwnerFaulted):
        await owner.aclose()
    assert disk_counts(tmp_path) == (1, 100)


async def test_cancelled_rollback_wait_stays_owned_and_close_is_bounded(owners, monkeypatch):
    owner = await owners(max_pending=1, wait_timeout=0.1)
    gate = FsyncGate(monkeypatch, call=3)

    async def unsent():
        async with owner.reserve(KEY, 50, 50):
            pass

    task = asyncio.create_task(unsent())
    try:
        await gate.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner.pending == 1
        with pytest.raises(TimeoutError):
            await owner.aclose()
        assert owner.pending == 1 and not gate.release.is_set()
        with pytest.raises(OwnerUnavailable):
            await dispatch(owner)
    finally:
        gate.release.set()
    await owner.aclose()
    restored = await owners()
    assert await restored.snapshot(KEY) == {"requests": 0, "tokens": 0}


async def test_cancelled_close_can_be_awaited_again(owners):
    owner = await owners()
    async with owner.reserve(KEY, 50, 50) as ticket:
        closing = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(OwnerUnavailable):
            ticket.handoff()
    await owner.aclose()


@pytest.mark.parametrize("defect", ["missing", "corrupt", "wrong-scope", "same-path", "alias"])
async def test_bad_custody_or_restore_fails_closed(tmp_path, defect):
    scope = scope_at(tmp_path, cold=False)
    scopes = [scope]
    if defect == "corrupt":
        (tmp_path / "ledger.json").write_text("not-json")
    if defect == "wrong-scope":
        pool = LedgerPool(scope.budgets, scope.state_path, clock=lambda: 1000.0)
        pool.check_and_debit(OTHER, 1, 1)
    if defect in {"same-path", "alias"}:
        scope = replace(scope, allow_cold_start=True)
        path = scope.state_path
        if defect == "alias":
            alias = tmp_path / "alias.json"
            alias.symlink_to(path)
            path = str(alias)
        scopes = [scope, replace(scope, key=OTHER, state_path=path)]
    owner = PersistenceOwner(scopes, max_pending=1, wait_timeout=2)
    try:
        with pytest.raises((OSError, ValueError)):
            await owner.start()
        with pytest.raises(OwnerFaulted):
            await dispatch(owner)
    finally:
        with pytest.raises(OwnerFaulted):
            await owner.aclose()


@pytest.mark.parametrize("pending,wait", [(0, 1), (True, 1), (1, 0), (1, float("inf")), (1, True)])
async def test_invalid_work_bounds_rejected(tmp_path, pending, wait):
    with pytest.raises(ValueError):
        PersistenceOwner([scope_at(tmp_path)], max_pending=pending, wait_timeout=wait)


async def test_unknown_scope_invalid_cost_and_unstarted_owner_do_not_submit(tmp_path):
    owner = PersistenceOwner([scope_at(tmp_path)], max_pending=1, wait_timeout=2)
    try:
        with pytest.raises(OwnerUnavailable):
            await dispatch(owner)
        await owner.start()
        with pytest.raises(ValueError, match="scope"):
            await dispatch(owner, key=OTHER)
        with pytest.raises(ValueError, match="integer"):
            async with owner.reserve(KEY, True, 50):
                pytest.fail("invalid input yielded")
        assert owner.pending == 0 and not owner.faulted
    finally:
        await owner.aclose()


async def test_concurrent_requests_cannot_split_the_durable_budget(owners, tmp_path):
    owner = await owners(max_pending=4)
    results = await asyncio.gather(
        *(dispatch(owner, amount=25) for _ in range(3)), return_exceptions=True
    )
    assert sum(isinstance(result, BudgetRefused) for result in results) == 1
    assert sorted(result.seq for result in results if not isinstance(result, Exception)) == [1, 2]
    assert owner.pending == 0 and not owner.faulted
    assert await owner.snapshot(KEY) == {"requests": 2, "tokens": 100}
    assert disk_counts(tmp_path) == (2, 100)


async def test_expired_unsent_rollback_does_not_fault_other_admission(owners):
    clock = [1000.0]
    owner = await owners(clock=lambda: clock[0])
    async with owner.reserve(KEY, 50, 50) as ticket:
        clock[0] = 1060.0
        assert await owner.snapshot(KEY) == {"requests": 0, "tokens": 0}
        with pytest.raises(OwnerUnavailable, match="expired"):
            ticket.handoff()
    assert not owner.faulted
    assert (await dispatch(owner)).seq == 2
