"""T002 — reservation ledger: deterministic acceptance (spec 245).

One calling thread owns the pool and its 60 s debit-time windows. Synthetic
budgets below are explicit test inputs, never provider defaults. These tests
also cover identity, strict transactional restore, late completion, and the
pool's persist-before-acknowledge boundary; they perform no upstream dispatch.
"""

import json

import pytest

from anthropic_throttle_proxy import ledger as ledger_module
from anthropic_throttle_proxy.ledger import Budgets, LaneLedger, LedgerPool

KEY = ("https://token-plan-sgp.xiaomimimo.com", "acct-a", "mimo-v2.6-pro")


class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def ledger():
    return LaneLedger(KEY, Budgets(10, 100), clock=FakeClock())


def test_debit_under_budget_grows_windows():
    clock = FakeClock()
    ledger = LaneLedger(KEY, Budgets(max_requests=10, max_tokens=1000), clock=clock)
    lease = ledger.check_and_debit(input_tokens=60, output_bound=40)
    assert lease is not None
    assert lease.reserved == 100
    snap = ledger.snapshot()
    assert snap["requests"] == 1 and snap["tokens"] == 100


def test_request_window_bound_refuses_over_rpm():
    clock = FakeClock()
    ledger = LaneLedger(KEY, Budgets(max_requests=2, max_tokens=10_000), clock=clock)
    assert ledger.check_and_debit(1, 1) is not None
    assert ledger.check_and_debit(1, 1) is not None
    assert ledger.check_and_debit(1, 1) is None  # 3rd within 60 s refused
    assert ledger.snapshot()["requests"] == 2  # refusal recorded nothing


def test_token_window_bound_counts_input_plus_output_bound(ledger):
    assert ledger.check_and_debit(input_tokens=60, output_bound=40) is not None
    assert ledger.check_and_debit(input_tokens=1, output_bound=1) is None


def test_rolling_window_expiry_restores_headroom():
    clock = FakeClock()
    ledger = LaneLedger(KEY, Budgets(max_requests=1, max_tokens=100), clock=clock)
    assert ledger.check_and_debit(50, 50) is not None
    assert ledger.check_and_debit(1, 1) is None
    clock.advance(61)
    assert ledger.check_and_debit(1, 1) is not None


def test_settle_refunds_only_known_unspent(ledger):
    lease = ledger.check_and_debit(50, 50)
    assert ledger.check_and_debit(50, 50) is None
    refunded = ledger.settle(lease, spent_tokens=20)
    assert refunded == 80  # reserved 100, known spend 20 -> 80 rolls back
    assert ledger.check_and_debit(40, 40) is not None  # 20 + 80 == 100 fits


def test_settle_preserves_known_overspend_liability(ledger):
    lease = ledger.check_and_debit(10, 10)
    assert ledger.settle(lease, spent_tokens=999) == 0
    assert ledger.snapshot()["tokens"] == 999
    assert ledger.check_and_debit(40, 40) is None
    assert ledger.settle(lease, spent_tokens=0) == 0  # no second refund
    assert ledger.snapshot()["tokens"] == 999


def test_retain_keeps_full_debt(ledger):
    lease = ledger.check_and_debit(50, 50)
    ledger.retain(lease)  # 429 / timeout / missing usage
    assert ledger.check_and_debit(50, 50) is None
    assert ledger.snapshot()["tokens"] == 100


def test_restart_debt_survives_reload_without_budget_shortcut(tmp_path):
    state = tmp_path / "ledger-state.json"
    clock = FakeClock()
    pool = LedgerPool(Budgets(max_requests=10, max_tokens=100), state_path=str(state), clock=clock)
    ledger = pool.ledger_for(KEY)
    assert ledger.check_and_debit(50, 50) is not None
    pool.save()
    # A new pool from the same file must NOT start with an empty full budget.
    pool2 = LedgerPool(Budgets(max_requests=10, max_tokens=100), state_path=str(state), clock=clock)
    ledger2 = pool2.ledger_for(KEY)
    assert ledger2.snapshot()["tokens"] == 100  # reserved 50+50 restored in full
    assert ledger2.check_and_debit(50, 50) is None
    saved = json.loads(state.read_text())
    assert saved["lanes"] and saved["lanes"][0]["entries"]


def test_settled_entries_expire_identically_after_restart(tmp_path):
    # The rolling rule is uniform: a settled entry expires 60 s after its debit
    # whether or not the process restarted in between.
    state = tmp_path / "ledger-state.json"
    clock = FakeClock()
    pool = LedgerPool(Budgets(max_requests=1, max_tokens=100), state_path=str(state), clock=clock)
    lease = pool.ledger_for(KEY).check_and_debit(50, 50)
    pool.ledger_for(KEY).settle(lease, spent_tokens=50)
    pool.save()
    pool2 = LedgerPool(Budgets(max_requests=1, max_tokens=100), state_path=str(state), clock=clock)
    clock.advance(61)
    assert pool2.ledger_for(KEY).check_and_debit(1, 1) is not None


@pytest.mark.parametrize(
    "budgets",
    [
        (0, 100),
        (10, 0),
        (-1, 100),
        (10, -1),
        (True, 100),
        (10, 2.5),
    ],
)
def test_invalid_budgets_refuse(budgets):
    with pytest.raises(ValueError):
        Budgets(*budgets)


@pytest.mark.parametrize("args", [(0, 1), (1, 0), (-5, 1), (1, -5), (True, 1), (1, 2.5)])
def test_invalid_reservation_amounts_refuse(args):
    ledger = LaneLedger(KEY, Budgets(10, 100), clock=FakeClock())
    assert ledger.check_and_debit(*args) is None


@pytest.mark.parametrize("other_key", [KEY, (KEY[0], "other-test-account", KEY[2])])
def test_foreign_lease_cannot_refund_or_retain(other_key):
    clock = FakeClock()
    first = LaneLedger(KEY, Budgets(10, 100), clock=clock, _id="a" * 32)
    second = LaneLedger(other_key, Budgets(10, 100), clock=clock, _id="b" * 32)
    foreign = first.check_and_debit(50, 50)
    own = second.check_and_debit(50, 50)
    assert foreign.seq == own.seq  # same sequence/time is not ledger identity
    with pytest.raises(ValueError):
        second.settle(foreign, 0)
    with pytest.raises(ValueError):
        second.retain(foreign)
    assert second.snapshot()["tokens"] == 100
    assert second.settle(own, 20) == 80


@pytest.mark.parametrize("spent", [False, True, 0.0, 0j, -1, "0", None, float("nan")])
def test_bad_settlement_is_rejected_before_mutation(spent):
    ledger = LaneLedger(KEY, Budgets(10, 100), clock=FakeClock())
    lease = ledger.check_and_debit(50, 50)
    with pytest.raises(ValueError):
        ledger.settle(lease, spent)
    assert ledger.snapshot() == {"requests": 1, "tokens": 100}
    assert ledger.settle(lease, 20) == 80  # invalid completion did not close it


@pytest.mark.parametrize("prune_first", [False, True])
def test_late_completion_is_independent_of_snapshot_order(prune_first):
    clock = FakeClock()
    ledger = LaneLedger(KEY, Budgets(10, 100), clock=clock)
    old = ledger.check_and_debit(50, 50)
    clock.advance(60)
    if prune_first:
        assert ledger.snapshot() == {"requests": 0, "tokens": 0}
    assert ledger.settle(old, 999) == 0  # no resurrection into another window
    ledger.retain(old)
    current = ledger.check_and_debit(50, 50)
    assert current.seq > old.seq
    assert ledger.settle(old, 0) == 0
    ledger.retain(old)
    assert ledger.snapshot() == {"requests": 1, "tokens": 100}


def _persisted_pool(tmp_path):
    state = tmp_path / "ledger.json"
    clock = FakeClock()
    pool = LedgerPool(Budgets(10, 100), str(state), clock)
    lease = pool.check_and_debit(KEY, 50, 50)
    assert lease is not None
    return state, clock, pool, lease


def test_restart_preserves_identity_and_expired_sequence_high_water(tmp_path):
    state, clock, pool, old = _persisted_pool(tmp_path)
    clock.advance(60)
    assert pool.ledger_for(KEY).snapshot()["requests"] == 0
    pool.save()
    restored = LedgerPool(pool.budgets, str(state), clock)
    current = restored.check_and_debit(KEY, 50, 50)
    assert current.ledger_id == old.ledger_id and current.seq > old.seq
    assert restored.ledger_for(KEY).settle(old, 0) == 0
    restored.ledger_for(KEY).retain(old)
    assert restored.ledger_for(KEY).snapshot()["tokens"] == 100


def test_saved_known_overspend_survives_restart(tmp_path):
    state, clock, pool, lease = _persisted_pool(tmp_path)
    assert pool.ledger_for(KEY).settle(lease, 999) == 0
    pool.save()  # settlement is explicitly provisional until this succeeds
    restored = LedgerPool(pool.budgets, str(state), clock)
    assert restored.ledger_for(KEY).snapshot()["tokens"] == 999
    assert restored.check_and_debit(KEY, 1, 1) is None


def _reject_corrupt_snapshot(state, clock, pool):
    lanes = pool.lanes
    with pytest.raises(ValueError):
        pool._load()
    assert pool.lanes is lanes
    assert pool.ledger_for(KEY).snapshot()["tokens"] == 100
    with pytest.raises(ValueError):
        LedgerPool(pool.budgets, str(state), clock)


def _saved_payload(tmp_path):
    state, clock, pool, _lease = _persisted_pool(tmp_path)
    return state, clock, pool, json.loads(state.read_text())


@pytest.mark.parametrize(
    "field,value",
    [
        ("seq", 0),
        ("seq", True),
        ("seq", 2),  # collides with persisted next_seq
        ("at", float("nan")),
        ("at", float("inf")),
        ("at", True),
        ("tokens", -100),
        ("tokens", 0),  # zero is not an unsettled reservation
        ("tokens", 0.0),
        ("tokens", False),
        ("settled", 1),
    ],
)
def test_malformed_entry_never_replaces_live_state(tmp_path, field, value):
    state, clock, pool, payload = _saved_payload(tmp_path)
    payload["lanes"][0]["entries"][0][field] = value
    state.write_text(json.dumps(payload))
    _reject_corrupt_snapshot(state, clock, pool)
    assert pool.ledger_for(KEY).check_and_debit(100, 100) is None


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"lanes":[]}',  # unversioned prototype is not a cold start
        '{"version":true,"lanes":[]}',
        '{"version":1,"lanes":null}',
        '{"version":1,"lanes":[],"lanes":[]}',
        "{",
    ],
)
def test_malformed_root_cannot_restore_empty_headroom(tmp_path, raw):
    state, clock, pool, _lease = _persisted_pool(tmp_path)
    state.write_text(raw)
    _reject_corrupt_snapshot(state, clock, pool)


@pytest.mark.parametrize(
    "defect", ["key", "id", "next_seq", "entries", "lane-dup", "id-dup", "seq-dup"]
)
def test_invalid_lane_or_duplicate_state_is_rejected(tmp_path, defect):
    state, clock, pool, payload = _saved_payload(tmp_path)
    lane = payload["lanes"][0]
    if defect == "lane-dup":
        payload["lanes"].append(lane)
    elif defect == "id-dup":
        payload["lanes"].append({**lane, "key": [KEY[0], "other-test-account", KEY[2]]})
    elif defect == "seq-dup":
        lane["entries"].append(lane["entries"][0])
    elif defect == "key":
        lane["key"] = "abc"
    elif defect == "id":
        lane["id"] = "not-a-ledger-identity"
    elif defect == "next_seq":
        lane["next_seq"] = True
    else:
        del lane["entries"]
    state.write_text(json.dumps(payload))
    _reject_corrupt_snapshot(state, clock, pool)


def test_restore_is_transactional_across_lanes(tmp_path):
    state, clock, pool, payload = _saved_payload(tmp_path)
    payload["lanes"][0]["entries"][0].update(tokens=0, settled=True)
    payload["lanes"].append({"key": ["invalid"]})
    state.write_text(json.dumps(payload))
    _reject_corrupt_snapshot(state, clock, pool)  # valid first row was not published


@pytest.mark.parametrize("failure", ["file-fsync", "replace", "directory-fsync"])
def test_failed_persistence_never_acknowledges_debit(tmp_path, monkeypatch, failure):
    state, clock, pool, _lease = _persisted_pool(tmp_path)
    # Advance the old reservation out of the window before injecting failures.
    clock.advance(60)
    pool.ledger_for(KEY).snapshot()
    pool.save()
    previous = state.read_bytes()
    fsync = ledger_module.os.fsync
    calls = 0
    acknowledged = []

    def broken_replace(*_args):
        raise OSError("injected replace failure")

    def failing_fsync(fd):
        nonlocal calls
        calls += 1
        target = 1 if failure == "file-fsync" else 2
        if calls == target:
            raise OSError("injected fsync failure")
        fsync(fd)

    with monkeypatch.context() as patch:
        if failure == "replace":
            patch.setattr(ledger_module.os, "replace", broken_replace)
        else:
            patch.setattr(ledger_module.os, "fsync", failing_fsync)
        with pytest.raises(OSError):
            acknowledged.append(pool.check_and_debit(KEY, 50, 50))
    assert acknowledged == []  # a caller cannot receive a dispatchable lease
    assert pool.ledger_for(KEY).snapshot()["tokens"] == 100  # conservative debt
    assert not list(tmp_path.glob(".ledger-*"))
    restored = LedgerPool(pool.budgets, str(state), clock)
    if failure == "directory-fsync":
        assert restored.ledger_for(KEY).snapshot()["tokens"] == 100
    else:
        assert state.read_bytes() == previous
        assert restored.ledger_for(KEY).snapshot()["tokens"] == 0


@pytest.mark.parametrize("spent", [0, 20])
def test_persisted_admission_requires_a_path_and_round_trips(tmp_path, spent):
    pool = LedgerPool(Budgets(10, 100), clock=FakeClock())
    with pytest.raises(ValueError):
        pool.check_and_debit(KEY, 50, 50)
    with pytest.raises(ValueError):
        pool.save()
    assert pool.lanes == {}
    state, clock, pool, lease = _persisted_pool(tmp_path)
    restored = LedgerPool(pool.budgets, str(state), clock)
    assert restored.ledger_for(KEY).snapshot()["tokens"] == lease.reserved
    assert restored.ledger_for(KEY).settle(lease, spent) == 100 - spent
    restored.save()
    again = LedgerPool(pool.budgets, str(state), clock)
    assert again.ledger_for(KEY).snapshot()["tokens"] == spent
