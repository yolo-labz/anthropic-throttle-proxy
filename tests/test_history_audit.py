"""Offline acceptance for the retained-history census and its missing-data boundary."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "scripts/token-usage-lake/research/history_audit.py"
SPEC = importlib.util.spec_from_file_location("history_audit", SOURCE)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
STAMP = "2026-11-14T18:00:00Z"
CUTOFF = audit.epoch("2026-11-15T00:00:00Z")


def event(kind="assistant", ident="e1", output=100, status="end_turn"):
    usage = {
        "input": 20,
        "output": output,
        "cacheRead": 30,
        "cacheWrite": 0,
        "reasoning": 40,
        "totalTokens": 50 + output,
    }
    base = {"id": ident, "timestamp": STAMP}
    if kind == "assistant":
        return {
            **base,
            "type": "message",
            "message": {
                "role": "assistant",
                "model": "fixture",
                "provider": "local",
                "content": [{"type": "text", "text": "PRIVATE body"}],
                "stopReason": status,
                "usage": usage,
            },
        }
    return {**base, "type": "compaction", "summary": "PRIVATE summary", "usage": usage}


def source(path, events, session="s1", suffix=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session", "id": session},
        {"type": "model_change", "modelId": "fixture", "provider": "local"},
        *events,
    ]
    path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in rows) + suffix)
    return path


def test_full_workload_denominator_includes_failures_and_not_extra_reasoning(tmp_path):
    p = source(
        tmp_path / "source.jsonl",
        [
            event(output=100),
            event(ident="failed", output=200, status="error"),
            event("compaction", "c1", 100),
        ],
    )
    result = audit.run([("pi", p)], tmp_path / "audit", CUTOFF, 50)
    row = result["opportunities"][0]
    assert row["events"] == 3
    assert row["output_observed"] == 400
    assert row["component_total_observed"] == 550
    assert row["cap_fraction_of_all_output"] == 0.125
    assert row["cap_fraction_of_all_component_tokens"] == 50 / 550
    assert result["realized_savings"] is None
    text = (tmp_path / "audit/events.sqlite").read_bytes()
    assert b"PRIVATE" not in text
    daily = json.loads((tmp_path / "audit/daily.json").read_text())
    assert daily[0]["kinds"]["assistant"]["status"]["error"] == 1


def test_copies_across_sessions_do_not_multiply_spend(tmp_path):
    a = source(tmp_path / "a.jsonl", [event()])
    b = source(tmp_path / "b.jsonl", [event()], session="fork")
    result = audit.run([("a", a), ("b", b), ("alias", a)], tmp_path / "audit", CUTOFF, 50)
    assert result["unique_events"] == 1
    assert result["capture"]["duplicate_copies"] == 1
    assert result["capture"]["duplicate_file_aliases"] == 1
    assert result["opportunities"][0]["output_observed"] == 100


def test_conflicting_rewrites_are_excluded_and_counted(tmp_path):
    a = source(tmp_path / "a.jsonl", [event()])
    b = source(tmp_path / "b.jsonl", [event(output=999)])
    result = audit.run([("a", a), ("b", b)], tmp_path / "audit", CUTOFF, 50)
    assert result["conflicted_events_excluded"] == 1
    assert result["opportunities"] == []


def test_missing_usage_is_not_zero_and_blocks_fraction(tmp_path):
    missing = event(ident="missing")
    del missing["message"]["usage"]
    p = source(tmp_path / "a.jsonl", [event("compaction", output=100), missing])
    result = audit.run([("a", p)], tmp_path / "audit", CUTOFF, 50)
    row = result["opportunities"][0]
    assert row["missing_output_events"] == 1
    assert row["cap_fraction_of_all_output"] is None
    assert row["cap_fraction_of_all_component_tokens"] is None


def test_frozen_boundary_and_invalid_tail_are_visible(tmp_path):
    p = source(tmp_path / "a.jsonl", [event()], suffix=b'{"unfinished":')
    result = audit.run([("a", p)], tmp_path / "audit", audit.epoch(STAMP), 50)
    assert result["unique_events"] == 0
    assert result["capture"]["incomplete_tail"] == 1


def test_missing_source_does_not_claim_zero_success(tmp_path):
    result = audit.run([("missing", tmp_path / "gone")], tmp_path / "audit", CUTOFF, 50)
    assert result["capture"]["read_errors"] == 1


def test_account_roots_and_recovery_are_discovered(tmp_path):
    for root in (
        "agent/sessions",
        "agent-gpt-a/sessions",
        "agent-gpt-b/sessions",
        "agent/recovery-archives",
    ):
        source(tmp_path / ".pi" / root / "x.jsonl", [event()])
    assert len(audit.discover(tmp_path)) == 4


@pytest.mark.parametrize("session,ident", [({}, "e1"), ("s1", []), ("", "e1")])
def test_invalid_id_types_fail_closed(session, ident):
    with pytest.raises(ValueError, match="unidentifiable"):
        audit.event_row(event(ident=ident), session, "fixture", "local", CUTOFF)


def test_rejects_nonfinite_bool_and_naive_dates():
    for value in (True, float("nan"), float("inf"), -1, "42"):
        assert audit.number(value) is None
    assert audit.epoch("2026-11-14T00:00:00") is None
    assert audit.epoch("bad") is None


def test_oversized_record_is_visible_not_half_parsed(tmp_path, monkeypatch):
    p = source(tmp_path / "a.jsonl", [event()])
    monkeypatch.setattr(audit, "MAX_LINE", 200)
    result = audit.run([("a", p)], tmp_path / "audit", CUTOFF, 50)
    assert result["capture"]["oversized_lines"] == 1
    assert result["unique_events"] == 0


def test_refuses_overwriting_a_previous_freeze(tmp_path):
    with pytest.raises(FileExistsError):
        audit.run([], tmp_path, CUTOFF, 50)


def test_unknown_and_zero_usage_are_distinct(tmp_path):
    p = source(tmp_path / "a.jsonl", [event(output=0)])
    result = audit.run([("a", p)], tmp_path / "audit", CUTOFF, 50)
    row = result["opportunities"][0]
    assert row["missing_output_events"] == 0
    assert row["cap_fraction_of_all_output"] is None
    db = sqlite3.connect(tmp_path / "audit/events.sqlite")
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    db.close()
