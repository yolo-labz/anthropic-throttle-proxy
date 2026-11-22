"""B1: producer rows carry clock and provenance fields; old rows read UNKNOWN (None)."""

import json
from pathlib import Path

from anthropic_throttle_proxy import meter_recorder as mr

SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "scripts/token-usage-lake/schemas/quota-meter-row.v2.schema.json"
)
NEW_FIELDS = (
    "recorder_observed_at",
    "proxy_header_observed_at",
    "clock_authority",
    "clock_uncertainty",
    "producer_revision",
    "duplicate_sample",
    "measured_or_projected",
)


def _state(at):
    return {
        "b": {
            "unified": {"util_5h": 0.1, "reset_5h": 100, "util_7d": 0.2, "reset_7d": 200},
            "unified_at": at,
        }
    }


def test_fresh_and_duplicate_rows_carry_every_new_field():
    rec = mr.MeterRecorder(Path("/nonexistent/m.jsonl"), lambda: _state(5.0))
    fresh = rec.rows(10.5)[0]
    dup = rec.rows(70.5)[0]
    for row in (fresh, dup):
        assert all(f in row for f in NEW_FIELDS), row
        assert row["recorder_observed_at"] in (10.5, 70.5)
        assert row["proxy_header_observed_at"] == 5.0
        assert row["clock_authority"] == "local_proxy_wallclock"
        assert row["measured_or_projected"] == "measured"
        assert row["producer_revision"] == mr.PRODUCER_REVISION
        # skew is not measured anywhere: unknown stays None, never 0
        assert row["clock_uncertainty"] is None
    assert fresh["duplicate_sample"] is False
    assert dup["duplicate_sample"] is True and dup["stale"] is True


def test_old_row_reads_unknown_never_false_or_zero():
    old = {"epoch": 10, "bearer": "b", "sampled_at": 5.0, "sample_age_s": 5.0, "util_7d": 0.2}
    got = mr.normalize_row(old)
    for f in NEW_FIELDS:
        assert got[f] is None, f
    # explicit stale=True is positive evidence of a duplicate; absence is not evidence of False
    assert mr.normalize_row({**old, "stale": True})["duplicate_sample"] is True
    assert mr.normalize_row({**old, "stale": False})["duplicate_sample"] is None
    assert "duplicate_sample" not in old  # input untouched


def test_new_row_roundtrips_through_the_log_unchanged(tmp_path):
    rec = mr.MeterRecorder(tmp_path / "m.jsonl", lambda: _state(5.0))
    rec.write(rec.rows(10.5))
    row = json.loads((tmp_path / "m.jsonl").read_text())
    assert {f: mr.normalize_row(row)[f] for f in NEW_FIELDS} == {f: row[f] for f in NEW_FIELDS}


def test_lake_contract_declares_every_field_nullable():
    props = json.loads(SCHEMA.read_text())["properties"]
    for f in NEW_FIELDS:
        assert f in props, f
        assert "null" in props[f]["type"], f"{f} must allow null (UNKNOWN)"
