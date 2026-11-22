"""prefix-rebuild attribution (scripts/token-usage-lake/analysis/prefix_rebuilds), synthetic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[1] / "scripts/token-usage-lake/analysis/prefix_rebuilds"
_spec = importlib.util.spec_from_file_location("attribute", _DIR / "attribute.py")
at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(at)

KEY = b"k" * 32
CUT = at.epoch("2026-11-08T21:44:01-03:00")


def t(hms: str) -> float:
    return at.epoch(f"2026-11-08T{hms}-03:00")


@pytest.fixture
def journal():
    with (_DIR / "fixtures/journal.synthetic.txt").open() as fh:
        return at.Journal(fh, KEY)


def _rec(prev: str, when: str, w1h: int | None = 0, cause: str = "unexplained") -> dict:
    return {
        "file": "F",
        "provider": "p",
        "model": "m1",
        "when": t(when),
        "prev": t(prev),
        "profiler_cause": cause,
        "cache_write": 100,
        "cache_write_1h": w1h,
    }


def test_journal_parses_metadata_only(journal) -> None:
    assert len(journal.migrates) == 1
    assert sum(map(len, journal.done.values())) == 6  # account-route lines are not 'done'
    blob = json.dumps(journal.done)
    assert "aaaa1111" not in blob and "s/one" not in blob  # aliased on read


def test_each_class(journal) -> None:
    seats = {"F": journal.alias("s/one")}

    def cls(r):
        return at.classify(r, journal, seats)

    assert cls(_rec("21:00:00", "21:00:33")) == "a_account_switch_migrate"
    assert cls(_rec("22:00:00", "22:01:00")) == "a2_account_switch_joined"
    assert cls(_rec("22:02:00", "22:03:00")) == "d_unexplained_same_account"
    assert cls(_rec("22:03:00", "22:10:00", w1h=0)) == "b_idle_past_ttl"
    assert cls(_rec("20:00:00", "21:30:00", w1h=100)) == "b_idle_past_ttl"  # past the 1h TTL
    assert cls(_rec("22:03:00", "22:10:00", w1h=100)) == "d_unexplained_unjoined"  # 1h applied


def test_migrate_window_is_5s_before_only(journal) -> None:
    assert journal.migrate_before(t("21:00:35"))
    assert not journal.migrate_before(t("21:00:36"))  # 6 s after
    assert not journal.migrate_before(t("21:00:29"))  # before the line


def test_unknown_seat_never_joins(journal) -> None:
    assert at.account(journal, {}, "F", "m1", t("22:01:00")) is None
    assert at.classify(_rec("22:00:00", "22:01:00"), journal, {}) == "d_unexplained_unjoined"


def test_ambiguous_seat_is_not_learned(journal) -> None:
    calls = [("F", "m1", t("22:00:00")), ("F", "m1", t("22:01:00")), ("G", "m1", t("23:59:00"))]
    seats = at.learn_seats(calls, journal)
    assert set(seats) == {"F"}  # G matched nothing


def test_summary_splits_pre_post_and_runs_control(journal) -> None:
    seats = {"F": journal.alias("s/one")}
    recs = [
        _rec("21:00:00", "21:00:33"),
        _rec("22:00:00", "22:01:00"),
        _rec("22:02:00", "22:03:00", cause="idle_past_cache_ttl"),
    ]
    doc = at.summarize(recs, journal, seats, CUT, {"rows_checked": 9})
    u = doc["by_profiler_cause"]["unexplained"]
    assert u["pre"]["real"]["a_account_switch_migrate"] == 1
    assert u["post"]["real"]["a2_account_switch_joined"] == 1
    assert "shift_600s_control" in u["post"]
    assert u["pre"]["cross"] == {"migrate5s=1,joined=switch": 1}
    late = at.summarize([_rec("21:43:00", "21:45:00")], journal, seats, CUT, {})
    assert set(late["by_profiler_cause"]["unexplained"]) == {"post_spans_restart"}
    assert "shift_600s_control" not in doc["by_profiler_cause"]["idle_past_cache_ttl"]["post"]
    assert doc["c_prompt_or_tool_churn"] == {
        "measurable": False,
        "rows_checked": 9,
        "matching_key_names": [],
    }
    assert "F" not in json.dumps(doc)  # no file alias in the output


def _line(**row) -> str:
    return json.dumps(row) + "\n"


def _asst(i: int, ts: str, read: int, write: int) -> str:
    msg = {
        "role": "assistant",
        "provider": "p",
        "model": "m1",
        "content": [],
        "stopReason": "stop",
        "usage": {
            "input": 1,
            "output": 1,
            "cacheRead": read,
            "cacheWrite": write,
            "totalTokens": 2,
        },
    }
    return _line(type="message", id=f"a{i}", parentId=None, timestamp=ts, message=msg)


def test_refuses_an_unpinned_profiler(tmp_path) -> None:
    fake = tmp_path / "profile_usage.py"
    fake.write_text("x = 1\n")
    with pytest.raises(SystemExit, match="refusing"):
        at.load_profiler(fake)


def test_true_falsifier_lists_only_both_post_cases(journal) -> None:
    seats = {"F": journal.alias("s/one")}
    recs = [
        {**_rec("22:00:00", "22:01:00"), "base": "x.jsonl"},  # post, switch -> listed
        {**_rec("22:02:00", "22:03:00"), "base": "y.jsonl"},  # post, same acct, unexplained
        {**_rec("21:43:00", "22:01:00"), "base": "z.jsonl"},  # spans restart -> not listed
        {**_rec("22:02:00", "22:03:00", cause="compaction"), "base": "c.jsonl"},
    ]
    got = at.falsifiers(recs, journal, seats, CUT)
    assert [(g["session_file"], g["bucket"]) for g in got] == [
        ("x.jsonl", "account_switch_by_join"),
        ("y.jsonl", "unexplained"),
    ]
    assert got[0]["call_at"] == "2026-11-08T22:01:00-03:00"
    assert set(got[0]) == {
        "session_file",
        "call_at",
        "previous_call_at",
        "bucket",
        "profiler_cause",
    }


def test_merge_sums_chunks_and_refuses_disagreement() -> None:
    a = {"x": {"n": 1, "s": "w"}, "l": ["k"], "m": False}
    b = {"x": {"n": 2, "s": "w", "o": 3}, "l": ["j"], "m": True}
    assert at.merge([a, b]) == {"x": {"n": 3, "s": "w", "o": 3}, "l": ["j", "k"], "m": True}
    with pytest.raises(ValueError, match="disagree"):
        at.merge([{"s": "a"}, {"s": "b"}])
