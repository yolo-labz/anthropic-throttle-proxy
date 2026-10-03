"""T002 — reservation ledger: per-(upstream, account, model) rolling windows.

Default-off, standalone slice (spec 245 tasks.md T002). No hot-path wiring
(T003 later), no strict activation, and NO provider budget, default or scope is
invented here: budgets are validated operator configuration and a request the
ledger cannot account for is refused, not guessed.

Contract:

* One process, one calling thread, one pool owner per state file. Synchronous
  operations cannot interleave on that thread; this is NOT a thread/process lock.
* Both windows last 60 s from debit. Late completion is a zero-refund no-op;
  it never resurrects expired accounting or touches a newer lease. This is a
  ledger policy, NOT evidence of a provider's long-stream charging semantics.
* LaneLedger operations are IN-MEMORY ONLY. LedgerPool.check_and_debit returns
  a lease only after save succeeds. A failed save raises, retains conservative
  in-memory debt, and acknowledges nothing: the caller MUST NOT dispatch.
* Settlement accepts exact non-negative integers, records full known spend
  (including overspend), and refunds only the known-unspent difference. Its
  mutation is provisional until an explicit pool.save; retain grants no refund.
* cancel_unsent removes both debits only when the caller proves no transport
  handoff. It rejects foreign/settled leases, preserves sequence high-water,
  and is provisional until save. The ledger cannot observe transport itself.
* Successful save uses a same-directory temporary file, file fsync, replace,
  then directory fsync. A post-replace failure can leave durable debt without
  acknowledgment, which is conservative. A missing file is a cold start, not
  proof that prior debt never existed; retention of that file is caller-owned.
* Restore validates the entire versioned snapshot before replacing live state.
  Unversioned prototype files are rejected, never treated as empty budgets.
* ponytail: persisted wall-clock windows assume a trustworthy clock; backward
  jumps can hold debt longer than 60 s and forward jumps can expire it early.
  A future calibrated clock/restart policy must precede strict activation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)
WINDOW_S = 60.0


def _pos_int(value: object) -> bool:
    """True only for a genuine positive int (bools and floats excluded)."""
    return type(value) is int and value > 0


def _valid_key(value: object) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(isinstance(part, str) and part.strip() for part in value)
    )


def _valid_time(value: object) -> bool:
    try:
        return type(value) in (int, float) and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("corrupt ledger state: duplicate field")
        result[key] = value
    return result


@dataclass(frozen=True)
class Budgets:
    """Operator-configured lane budgets; nothing here is a vendor default."""

    max_requests: int
    max_tokens: int

    def __post_init__(self) -> None:
        if not _pos_int(self.max_requests) or not _pos_int(self.max_tokens):
            raise ValueError("budgets must be positive integers")


@dataclass(frozen=True)
class Lease:
    """One reservation: what was debited and when."""

    key: tuple[str, str, str]
    seq: int
    input_tokens: int
    output_bound: int
    at: float
    ledger_id: str

    @property
    def reserved(self) -> int:
        return self.input_tokens + self.output_bound


@dataclass
class _Entry:
    seq: int
    at: float
    tokens: int
    settled: bool = False


@dataclass
class LaneLedger:
    """Rolling windows + debt for one (upstream, account, model)."""

    key: tuple[str, str, str]
    budgets: Budgets
    clock: Callable[[], float] = time.time
    entries: list[_Entry] = field(default_factory=list)
    _next_seq: int = 1
    _id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not isinstance(self.key, tuple) or not _valid_key(self.key):
            raise ValueError("lane key must be three non-empty strings")

    def _now(self) -> float:
        now = self.clock()
        if not _valid_time(now):
            raise ValueError("clock must return a finite non-negative timestamp")
        return now

    def _prune(self, now: float) -> None:
        self.entries = [entry for entry in self.entries if now - entry.at < WINDOW_S]

    def _entry_for(self, lease: Lease) -> _Entry | None:
        if (
            not isinstance(lease, Lease)
            or lease.key != self.key
            or lease.ledger_id != self._id
            or not _pos_int(lease.seq)
            or lease.seq >= self._next_seq
            or not _valid_time(lease.at)
        ):
            raise ValueError("lease does not belong to this ledger")
        now = self._now()
        self._prune(now)
        # Sequence high-water + ledger identity survive pruning and restart.
        # Expired callbacks are harmless even if a snapshot pruned first.
        if now - lease.at >= WINDOW_S:
            return None
        for entry in self.entries:
            if entry.seq == lease.seq and entry.at == lease.at:
                return entry
        raise ValueError("lease is not outstanding in this ledger")

    def check_and_debit(self, input_tokens: int, output_bound: int) -> Lease | None:
        """Provisional in-memory debit on the owning thread; None refuses."""
        if not _pos_int(input_tokens) or not _pos_int(output_bound):
            return None
        reserved = input_tokens + output_bound
        now = self._now()
        self._prune(now)
        if len(self.entries) >= self.budgets.max_requests:
            return None
        if sum(entry.tokens for entry in self.entries) + reserved > self.budgets.max_tokens:
            return None
        entry = _Entry(seq=self._next_seq, at=now, tokens=reserved)
        self._next_seq += 1
        self.entries.append(entry)
        return Lease(
            key=self.key,
            seq=entry.seq,
            input_tokens=input_tokens,
            output_bound=output_bound,
            at=entry.at,
            ledger_id=self._id,
        )

    def settle(self, lease: Lease, spent_tokens: int) -> int:
        """Provisional known spend; expired/repeated completion refunds zero."""
        if type(spent_tokens) is not int or spent_tokens < 0:
            raise ValueError("spent_tokens must be a non-negative integer")
        entry = self._entry_for(lease)
        if entry is None or entry.settled:
            return 0
        refund = max(0, entry.tokens - spent_tokens)
        entry.tokens = spent_tokens  # known overspend is liability, not a discount
        entry.settled = True
        return refund

    def cancel_unsent(self, lease: Lease) -> int:
        """Provisional rollback ONLY for caller-proven unsent work.

        Remove RPM as well as tokens, unlike settle(lease, 0). An expired
        legitimate lease is a no-op; a repeated active cancellation raises
        rather than granting a second refund. Persist before reusing headroom.
        """
        entry = self._entry_for(lease)
        if entry is None:
            return 0
        if entry.settled:
            raise ValueError("a settled lease cannot be cancelled as unsent")
        self.entries.remove(entry)
        return entry.tokens

    def retain(self, lease: Lease) -> None:
        """429 / timeout / missing usage: the full reservation stands."""
        self._entry_for(lease)  # validate the lease; numbers deliberately unchanged

    def snapshot(self) -> dict:
        self._prune(self._now())
        return {
            "requests": len(self.entries),
            "tokens": sum(entry.tokens for entry in self.entries),
        }


def _parse_entry(raw: object, next_seq: int, sequences: set[int]) -> _Entry:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"seq", "at", "tokens", "settled"}
        or not _pos_int(raw["seq"])
        or raw["seq"] >= next_seq
        or raw["seq"] in sequences
        or not _valid_time(raw["at"])
        or type(raw["tokens"]) is not int
        or raw["tokens"] < 0
        or type(raw["settled"]) is not bool
        or (not raw["settled"] and raw["tokens"] == 0)
    ):
        raise ValueError("corrupt ledger state: invalid entry")
    return _Entry(**raw)


def _parse_lane(raw: object, budgets: Budgets, clock: Callable[[], float]) -> LaneLedger:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"key", "id", "next_seq", "entries"}
        or not isinstance(raw["key"], list)
        or not _valid_key(raw["key"])
        or not isinstance(raw["id"], str)
        or len(raw["id"]) != 32
        or any(char not in "0123456789abcdef" for char in raw["id"])
        or not _pos_int(raw["next_seq"])
        or not isinstance(raw["entries"], list)
    ):
        raise ValueError("corrupt ledger state: invalid lane")
    entries = []
    sequences = set()
    for value in raw["entries"]:
        entry = _parse_entry(value, raw["next_seq"], sequences)
        entries.append(entry)
        sequences.add(entry.seq)
    return LaneLedger(
        key=tuple(raw["key"]),
        budgets=budgets,
        clock=clock,
        entries=entries,
        _next_seq=raw["next_seq"],
        _id=raw["id"],
    )


@dataclass
class LedgerPool:
    """Registry of lane ledgers with restart-debt persistence."""

    budgets: Budgets
    state_path: str | None = None
    clock: Callable[[], float] = time.time
    lanes: dict[tuple[str, str, str], LaneLedger] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_path:
            try:
                self._load()
            except FileNotFoundError:
                _LOG.warning(
                    "Reservation snapshot missing: explicit cold start with no restored debt"
                )

    def ledger_for(self, key: tuple[str, str, str]) -> LaneLedger:
        if not isinstance(key, tuple) or not _valid_key(key):
            raise ValueError("lane key must be three non-empty strings")
        ledger = self.lanes.get(key)
        if ledger is None:
            ledger = LaneLedger(key=key, budgets=self.budgets, clock=self.clock)
            self.lanes[key] = ledger
        return ledger

    def check_and_debit(
        self, key: tuple[str, str, str], input_tokens: int, output_bound: int
    ) -> Lease | None:
        """Acknowledge only a persisted debit; save failure raises, never dispatch.

        Do not combine this API with unsaved low-level mutations. Settle first,
        then explicitly save, before making further admission decisions.
        """
        if not self.state_path:
            raise ValueError("persisted admission requires a state_path")
        lease = self.ledger_for(key).check_and_debit(input_tokens, output_bound)
        if lease is not None:
            self.save()
        return lease

    def save(self) -> None:
        """Commit the explicit snapshot, or raise; one process/thread owns the file."""
        if not self.state_path:
            raise ValueError("persistence requires a state_path")
        payload = {
            "version": 1,
            "lanes": [
                {
                    "key": list(ledger.key),
                    "id": ledger._id,
                    "next_seq": ledger._next_seq,
                    "entries": [
                        {
                            "seq": entry.seq,
                            "at": entry.at,
                            "tokens": entry.tokens,
                            "settled": entry.settled,
                        }
                        for entry in ledger.entries
                    ],
                }
                for ledger in self.lanes.values()
            ],
        }
        parent = os.path.dirname(os.path.abspath(self.state_path))
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=parent, prefix=".ledger-", delete=False
            ) as handle:
                tmp = handle.name
                json.dump(payload, handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if tmp is not None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)

    def _load(self) -> None:
        with open(self.state_path, encoding="utf-8") as handle:
            payload = json.load(handle, object_pairs_hook=_unique_object)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "lanes"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
            or not isinstance(payload["lanes"], list)
        ):
            raise ValueError("corrupt ledger state: invalid schema")
        lanes = {}
        identities = set()
        for value in payload["lanes"]:
            lane = _parse_lane(value, self.budgets, self.clock)
            if lane.key in lanes or lane._id in identities:
                raise ValueError("corrupt ledger state: duplicate lane")
            lanes[lane.key] = lane
            identities.add(lane._id)
        # Nothing is published until every lane and entry has passed validation.
        self.lanes = lanes
