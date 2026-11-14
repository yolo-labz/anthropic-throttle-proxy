"""Per-request usage ledger: row shape, off switch, refusal rows, writer safety."""

import json
import time

import pytest

from anthropic_throttle_proxy import config
from anthropic_throttle_proxy import usage_ledger as ul


def _sse(*events):
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


START = {
    "type": "message_start",
    "message": {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 1,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 2,
        }
    },
}
DELTA = {"type": "message_delta", "usage": {"output_tokens": 42}}
_TAIL_9000 = _sse({"type": "message_delta", "usage": {"output_tokens": 9000}}).decode()
BLOCK = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}


def _wait_for_writes(path, before, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = ul.stats()
        if s["written"] + s["write_errors"] > before:
            return
        time.sleep(0.01)
    raise AssertionError("ledger writer did not drain")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "usage-ledger.jsonl"
    monkeypatch.setattr(ul, "default_path", lambda: path)
    monkeypatch.setattr(config, "USAGE_LEDGER_MODE", "observe")
    return path


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_fixture_sse_yields_exactly_one_row_with_counts(ledger):
    body = _sse(START, BLOCK, BLOCK, DELTA)
    before = ul.stats()["written"] + ul.stats()["write_errors"]
    ul.record(bearer="abcd1234", model="claude-opus-5", project="p1", status=200, captured=body)
    _wait_for_writes(ledger, before)
    rows = _rows(ledger)
    assert len(rows) == 1
    row = rows[0]
    assert row["bearer"] == "abcd1234"
    assert row["project"] == "p1"
    assert (row["input"], row["output"], row["cacheRead"], row["cacheWrite"]) == (10, 42, 5, 2)
    assert row["usage_complete"] is True
    assert "text" not in json.dumps(row)  # never prompt content


def test_refusal_writes_status_row_without_usage(ledger):
    body = b'{"type":"error","error":{"type":"rate_limit_error","message":"Error"}}'
    before = ul.stats()["written"] + ul.stats()["write_errors"]
    ul.record(bearer="abcd1234", model="claude-opus-5", project=None, status=429, captured=body)
    _wait_for_writes(ledger, before)
    (row,) = _rows(ledger)
    assert row["status"] == 429
    assert row["project"] == "_direct"
    assert "output" not in row and "input" not in row


def test_off_writes_nothing(ledger, monkeypatch):
    monkeypatch.setattr(config, "USAGE_LEDGER_MODE", "off")
    ul.record(bearer="abcd1234", model="m", project=None, status=200, captured=_sse(START, DELTA))
    time.sleep(0.2)
    assert not ledger.exists()


def test_writer_failure_never_raises(ledger, monkeypatch):
    def boom(path, rows):
        raise OSError("disk full")

    monkeypatch.setattr(ul, "write_rows", boom)
    before = ul.stats()["write_errors"]
    ul.record(bearer="abcd1234", model="m", project=None, status=200, captured=_sse(START, DELTA))
    deadline = time.time() + 3
    while ul.stats()["write_errors"] == before and time.time() < deadline:
        time.sleep(0.01)
    assert ul.stats()["write_errors"] > before


def test_output_is_final_cumulative_value_from_tail(ledger):
    # Head capture (1 MiB) holds message_start only; the terminal message_delta
    # sits in the tail. Output must be the tail's 9000, not the head's 1.
    head = _sse(START, BLOCK)
    tail = _TAIL_9000
    counts, complete = ul.parse_usage(head, tail)
    assert counts["output"] == 9000
    assert counts["input"] == 10
    assert complete is True


def test_mutant_snapshot_at_message_start_fails(ledger):
    # Mutant: a row written before the stream ends reads output_tokens=1.
    counts, complete = ul.parse_usage(_sse(START, BLOCK))
    assert counts["output"] != 42
    assert complete is False


def test_non_stream_json_body(ledger):
    body = json.dumps(
        {"type": "message", "usage": {"input_tokens": 3, "output_tokens": 7}}
    ).encode()
    counts, complete = ul.parse_usage(body)
    assert counts == {"input": 3, "output": 7, "cacheRead": 0, "cacheWrite": 0}
    assert complete is True


def test_unknown_mode_fails_closed():
    assert ul.parse_mode("observe") == "observe"
    assert ul.parse_mode("ON") == "off"
    assert ul.parse_mode(None) == "off"


def test_record_uses_tail_for_overflowed_stream(ledger):
    head = _sse(START, BLOCK)
    tail = _TAIL_9000
    before = ul.stats()["written"] + ul.stats()["write_errors"]
    ul.record(bearer="abcd1234", model="m", project=None, status=200, captured=head, tail=tail)
    _wait_for_writes(ledger, before)
    (row,) = _rows(ledger)
    assert row["output"] == 9000
    assert row["usage_complete"] is True
