"""State-persistence hardening tests (budget/state-persistence slice, 19/10/2026).

Every test drives the REAL persistence code through a tmp state file and a
simulated process restart (module-global cache + limiter registry reset).
Nothing here ever reads or writes the operator's live state files.

Findings these tests pin (audit: docs/STATE-PERSISTENCE-AUDIT-2026-10-19.md):

* Non-finite deadlines (JSON ``NaN``/``Infinity``, accepted by Python's
  ``json.loads``) previously survived the load filter and crashed the
  REQUEST PATH via ``int(nan)`` inside ``_restore_retry_after``.
* Expired entries were pruned from memory on load but lingered on disk
  forever (measured: a 39-day-old entry in the anthropic gate's file).
* The persist path wrote with a fixed ``.tmp`` name and NO fsync — the
  ledger's save path fsyncs file + directory; the retry-after writer did not.
* ``THROTTLE_RETRY_AFTER_STATE_FILE`` was opt-in-with-empty-default, so the
  go (:8770) and zen (:8774) gates persisted NOTHING: the 19/10 18:48:57
  restart forgot every noted window and re-descended the AIMD ladder with
  real 429s (18:49:02 max_concurrent=8 → 5 → 3 → 2 → 1).
* The ledger's documented clock ponytail: a backward clock jump between save
  and restore leaves ``entry.at`` in the future, holding budget debt past
  one window (and a corrupt far-future ``at`` held it forever).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time

import pytest

from anthropic_throttle_proxy import config, limiter
from anthropic_throttle_proxy.ledger import Budgets, LedgerPool
from anthropic_throttle_proxy.proxy import _get_bearer_limiter

KEY = ("opencode-zen", "mimo-free", "mimo-v2.6-flash")


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wire(tmp_path, monkeypatch):
    """Point the limiter persistence at a fresh tmp file, cache cold."""
    state_file = tmp_path / "retry-after-state.json"
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(state_file))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    return state_file


# ── F2: non-finite deadlines must never reach the request path ──────────────


def test_nan_deadline_dropped_and_restore_never_crashes(tmp_path, monkeypatch):
    """A NaN deadline used to survive the filter and raise ``int(nan)``.

    ``float('nan') <= now`` is False, so the expiry check passed it;
    ``nan > now + cap`` is False, so the restore cap never clamped it;
    ``_restore_retry_after`` then crashed the request path with
    ``ValueError: cannot convert float NaN to integer``.
    """
    state_file = _wire(tmp_path, monkeypatch)
    state_file.write_text('{"bid-nan": NaN}', encoding="utf-8")

    state = limiter._load_retry_after_state()
    assert state == {}  # rejected at the trust boundary
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk == {}  # self-healed: the poison is scrubbed from disk too

    lim = limiter.FairBearerLimiter(4, "fair", "bid-nan")
    limiter._restore_retry_after(lim, "bid-nan")  # must not raise
    assert lim.retry_after_remaining() == 0.0
    assert math.isfinite(lim._retry_after_until)


def test_infinite_deadline_is_bounded_not_restored(tmp_path, monkeypatch):
    state_file = _wire(tmp_path, monkeypatch)
    state_file.write_text('{"bid-inf": Infinity}', encoding="utf-8")

    state = limiter._load_retry_after_state()
    assert all(math.isfinite(v) for v in state.values())

    limiter._retry_after_state = None  # simulated restart
    lim = limiter.FairBearerLimiter(4, "fair", "bid-inf")
    limiter._restore_retry_after(lim, "bid-inf")
    remaining = lim.retry_after_remaining()
    assert math.isfinite(remaining)
    assert remaining <= config.RETRY_AFTER_RESTORE_CAP_S


async def test_request_path_survives_corrupt_state_file(tmp_path, monkeypatch):
    """End-to-end: the crash used to happen inside ``_get_bearer_limiter``."""
    _wire(tmp_path, monkeypatch)
    (tmp_path / "retry-after-state.json").write_text('{"bid1": NaN}', encoding="utf-8")
    limiter.set_lock(asyncio.Lock())

    lim = await _get_bearer_limiter("bid1", "fair", 3)

    assert lim.retry_after_remaining() == 0.0


def test_nonfinite_note_is_ignored_and_never_persisted(tmp_path, monkeypatch):
    """``note_retry_after(inf)`` used to latch an infinite wall window whose
    ``wait_retry_after`` would ``asyncio.sleep(inf)`` forever."""
    state_file = _wire(tmp_path, monkeypatch)
    lim = limiter.FairBearerLimiter(4, "fair", "bid1")
    before = lim._retry_after_until

    lim.note_retry_after(float("nan"))
    lim.note_retry_after(float("inf"))

    assert lim._retry_after_until == before
    assert math.isfinite(lim.retry_after_remaining())
    if state_file.exists():
        raw = state_file.read_text(encoding="utf-8")
        assert "Infinity" not in raw and "NaN" not in raw


# ── F3: stale entries must be swept from disk, not just from memory ─────────


def test_expired_entries_swept_from_disk_on_load(tmp_path, monkeypatch):
    """Measured 19/10: the anthropic gate's file still held an entry that
    expired ~39 days earlier — pruned from memory, never from disk."""
    state_file = _wire(tmp_path, monkeypatch)
    now = time.time()
    state_file.write_text(
        json.dumps({"dead42d": now - 3_600_000, "live5m": now + 300}),
        encoding="utf-8",
    )

    state = limiter._load_retry_after_state()

    assert "dead42d" not in state and "live5m" in state
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert "dead42d" not in on_disk  # the file self-heals at load
    assert "live5m" in on_disk


def test_all_expired_file_rewritten_empty(tmp_path, monkeypatch):
    state_file = _wire(tmp_path, monkeypatch)
    state_file.write_text(json.dumps({"dead": time.time() - 60}), encoding="utf-8")

    assert limiter._load_retry_after_state() == {}
    assert json.loads(state_file.read_text(encoding="utf-8")) == {}


# ── F1: state file resolution (config default flip) ─────────────────────────


def test_state_file_default_is_xdg_scoped(monkeypatch, tmp_path):
    monkeypatch.delenv("THROTTLE_RETRY_AFTER_STATE_FILE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert config._resolve_retry_after_state_file() == str(
        tmp_path / "anthropic-throttle-proxy" / "retry-after-state.json"
    )


def test_state_file_falls_back_to_home_when_no_xdg(monkeypatch):
    monkeypatch.delenv("THROTTLE_RETRY_AFTER_STATE_FILE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    resolved = config._resolve_retry_after_state_file()
    assert resolved.endswith(
        os.path.join(".local", "state", "anthropic-throttle-proxy", "retry-after-state.json")
    )


@pytest.mark.parametrize("value", ["off", "OFF", "none", "disabled", ""])
def test_state_file_explicit_opt_out(monkeypatch, value):
    """Stateless containers/tests keep the legacy side-effect-free default."""
    monkeypatch.setenv("THROTTLE_RETRY_AFTER_STATE_FILE", value)
    assert config._resolve_retry_after_state_file() == ""


def test_state_file_explicit_path_wins(monkeypatch, tmp_path):
    pinned = tmp_path / "pinned.json"
    monkeypatch.setenv("THROTTLE_RETRY_AFTER_STATE_FILE", str(pinned))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert config._resolve_retry_after_state_file() == str(pinned)


def test_opt_out_disables_persistence_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", "")
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    lim = limiter.FairBearerLimiter(4, "fair", "bid1")

    lim.note_retry_after(60)

    assert lim.retry_after_remaining() > 55
    # no state file was created (tmp_path also holds the suite's fleet-ui
    # fixture file, so assert the specific path, not the directory)
    assert not (tmp_path / "retry-after-state.json").exists()


# ── F5: durable atomic persist (fsync + unique tmp) ─────────────────────────


def test_persist_fsyncs_file_and_directory(tmp_path, monkeypatch):
    """The ledger's save fsyncs file + dir; the retry-after writer did not —
    a power loss right after ``os.replace`` could persist an empty file and
    silently forget every window (the cold start the feature exists to stop)."""
    _wire(tmp_path, monkeypatch)
    fsyncs: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(limiter.os, "fsync", counting_fsync)

    limiter._persist_retry_after_until("bid1", time.time() + 60)

    assert len(fsyncs) >= 2  # file content + containing directory
    assert (tmp_path / "retry-after-state.json").exists()
    assert not list(tmp_path.glob("*.tmp"))  # no residue


def test_persist_uses_unique_tmp_name_per_write(tmp_path, monkeypatch):
    """Two writers sharing one state file must not share one tmp path."""
    _wire(tmp_path, monkeypatch)
    seen: list[str] = []
    real_replace = os.replace

    def spy_replace(src, dst) -> None:
        seen.append(os.path.basename(os.fspath(src)))
        real_replace(src, dst)

    monkeypatch.setattr(limiter.os, "replace", spy_replace)

    limiter._persist_retry_after_until("bid1", time.time() + 60)
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    limiter._persist_retry_after_until("bid1", time.time() + 90)

    assert len(seen) == 2
    assert seen[0] != seen[1]  # unique per-write temp name


def test_persist_write_failure_never_raises_into_note(tmp_path, monkeypatch):
    """The never-raises contract: a full disk must not kill the note path."""
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(limiter.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("ENOSPC")))
    lim = limiter.FairBearerLimiter(4, "fair", "bid1")

    until = lim.note_retry_after(60)

    assert until > time.time() + 55  # the live window still holds
    assert math.isfinite(lim.retry_after_remaining())


# ── cross-restart round trip (the 18:48 incident shape) ─────────────────────


def test_pushback_window_survives_simulated_restart(tmp_path, monkeypatch):
    """Note → cold restart → window restored (bounded by the restore cap)."""
    _wire(tmp_path, monkeypatch)
    lim = limiter.FairBearerLimiter(4, "fair", "bid1")
    lim.note_retry_after(120)
    assert lim.retry_after_remaining() > 100

    # simulated restart: module cache + registry dropped, new limiter built
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    restored = limiter.FairBearerLimiter(4, "fair", "bid1")
    limiter._restore_retry_after(restored, "bid1")

    assert restored.retry_after_remaining() > 90
    assert restored.retry_probe_required()  # probe gate re-armed from disk


def test_restart_with_stale_file_starts_clean(tmp_path, monkeypatch):
    """A restart whose only window already expired starts open, not poisoned."""
    _wire(tmp_path, monkeypatch)
    state_file = tmp_path / "retry-after-state.json"
    state_file.write_text(json.dumps({"bid1": time.time() - 5}), encoding="utf-8")
    monkeypatch.setattr(limiter, "_retry_after_state", None)

    lim = limiter.FairBearerLimiter(4, "fair", "bid1")
    limiter._restore_retry_after(lim, "bid1")

    assert lim.retry_after_remaining() == 0.0


# ── F4: ledger clock-skew (backward jump / corrupt future at) ───────────────


def _saved_single_debit(tmp_path):
    """Persist one debit made at t=1000 against a one-request budget."""
    state = tmp_path / "ledger.json"
    pool = LedgerPool(Budgets(max_requests=1, max_tokens=1_000), str(state), FakeClock(1_000.0))
    assert pool.check_and_debit(KEY, 10, 90) is not None
    pool.save()
    return state


def test_future_entry_clamped_on_restore_bounds_backward_jump(tmp_path):
    """Backward clock jump between save and restart (or a corrupt far-future
    ``at``): the entry used to stay "younger than now" until the clock caught
    up, holding budget debt past one window — forever for a corrupt value."""
    state = _saved_single_debit(tmp_path)

    # restart with the clock 100 s BEHIND the save point
    back = FakeClock(900.0)
    restored = LedgerPool(Budgets(max_requests=1, max_tokens=1_000), str(state), back)
    lane = restored.ledger_for(KEY)

    assert len(lane.entries) == 1
    assert lane.entries[0].at == 900.0  # clamped to restore-time now
    back.advance(61)
    assert lane.check_and_debit(10, 90) is not None  # freed within one window


def test_corrupt_far_future_entry_never_permanently_blocks_budget(tmp_path):
    """A hand-edited ``at`` far in the future held the lane's single request
    slot against ``max_requests=1`` permanently (until the clock reached it)."""
    state = _saved_single_debit(tmp_path)
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["lanes"][0]["entries"][0]["at"] = 1e18
    state.write_text(json.dumps(payload), encoding="utf-8")

    clock = FakeClock(1_000.0)
    restored = LedgerPool(Budgets(max_requests=1, max_tokens=1_000), str(state), clock)
    clock.advance(61)
    assert restored.ledger_for(KEY).check_and_debit(10, 90) is not None


def test_forward_clock_jump_expires_debt_early_is_pinned(tmp_path):
    """Documented residual (unchanged): a FORWARD jump expires debt early.
    Unfixable from a wall-clock snapshot alone; the ledger stays default-off
    until a calibrated clock policy lands (spec 245 T002 contract)."""
    state = tmp_path / "ledger.json"
    clock = FakeClock(1_000.0)
    pool = LedgerPool(Budgets(max_requests=1, max_tokens=1_000), str(state), clock)
    assert pool.check_and_debit(KEY, 10, 90) is not None
    pool.save()

    ahead = FakeClock(1_000.0 + 3_600)  # clock jumped forward 1 h
    restored = LedgerPool(Budgets(max_requests=1, max_tokens=1_000), str(state), ahead)

    # Prune is lazy (next operation), and the 1 h-old reservation expires
    # immediately at that operation: the headroom is granted a window early.
    assert restored.ledger_for(KEY).check_and_debit(10, 90) is not None
