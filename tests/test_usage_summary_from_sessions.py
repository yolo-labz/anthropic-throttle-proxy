"""usage_summary_from_sessions.build on a synthetic transcript (no real sessions read)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts/token-usage-lake/export/usage_summary_from_sessions.py"
)
_spec = importlib.util.spec_from_file_location("usfs", _PATH)
usfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usfs)

NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.UTC)


def _line(**entry) -> str:
    return json.dumps(entry) + "\n"


def test_build_folds_calls_and_keeps_unreported_classes_unknown(tmp_path: Path) -> None:
    root = tmp_path / "sessions" / "slug"
    root.mkdir(parents=True)
    ts = "2026-11-09T11:00:00.000Z"
    msg = {"provider": "p", "model": "m"}
    (root / "s.jsonl").write_text(
        _line(type="session_info", timestamp=ts, name="seat-a")
        + _line(
            type="message",
            timestamp=ts,
            message={**msg, "usage": {"input": 10, "output": 5, "cacheRead": 100}},
        )
        + _line(
            type="message",
            timestamp=ts,
            message={**msg, "usage": {"input": 1, "output": 2, "cacheRead": 3}},
        ),
        encoding="utf-8",
    )
    doc = usfs.build([tmp_path / "sessions"], 24.0, now=NOW)
    assert doc["schema"] == usfs.SCHEMA
    assert doc["coverage"]["files_scanned"] == 1
    (row,) = doc["rows"]
    assert (row["pane"], row["role"], row["requests"]) == ("seat-a", "turn", 2)
    assert (row["fresh_input"], row["output"], row["cache_read"]) == (11, 7, 103)
    assert row["cache_write"] is None  # never reported: unknown, not zero
