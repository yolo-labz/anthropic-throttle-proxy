"""Deterministic ledger batch accounting, isolated from the live writer."""

import importlib.util
import queue

import pytest

from anthropic_throttle_proxy import usage_ledger


class _OneBatch(queue.Queue):
    def get(self, block=True, timeout=None):
        if block and self.empty():
            raise StopIteration
        return super().get(block=block, timeout=timeout)


@pytest.mark.parametrize(
    ("persisted_count", "raises", "expected_written", "expected_unconfirmed"),
    [(3, False, 3, 0), (0, True, 0, 3), (1, True, 0, 3)],
)
def test_write_exception_counts_unconfirmed_rows(
    monkeypatch, tmp_path, persisted_count, raises, expected_written, expected_unconfirmed
):
    # Fresh module globals avoid disturbing a background writer from another test.
    spec = importlib.util.spec_from_file_location(
        "anthropic_throttle_proxy._ledger_accounting_fixture", usage_ledger.__file__
    )
    assert spec is not None and spec.loader is not None
    ledger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ledger)
    items = _OneBatch()
    for i in range(3):
        items.put({"synthetic_id": i})
    monkeypatch.setattr(ledger, "_queue", items)
    monkeypatch.setattr(ledger, "default_path", lambda: tmp_path / "unused.jsonl")
    persisted = []

    def write(_path, rows):
        persisted.extend(rows[:persisted_count])
        if raises:
            raise OSError("synthetic failure, potentially after partial append")

    monkeypatch.setattr(ledger, "write_rows", write)
    with pytest.raises(StopIteration):
        ledger._drain()
    stats = ledger.stats()
    assert len(persisted) == persisted_count
    assert stats["written"] == expected_written
    assert stats["write_errors"] == int(raises)
    assert stats.get("write_unconfirmed_rows", 0) == expected_unconfirmed
    assert stats["dropped"] == 0
