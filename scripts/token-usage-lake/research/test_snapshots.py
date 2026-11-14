"""Run: python -m unittest discover -s scripts/token-usage-lake/research -v."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import snapshots


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lake = self.root / "lake"
        self.out = self.root / "snapshots"
        (self.lake / "runtime").mkdir(parents=True)
        self.db = self.lake / "lake.duckdb"
        with duckdb.connect(str(self.db)) as con:
            con.execute(
                "CREATE TABLE calls(at_brt TIMESTAMP, provider VARCHAR, model VARCHAR, "
                "effort_used VARCHAR, input BIGINT, output BIGINT, "
                "cache_read BIGINT, cache_write BIGINT)"
            )
            con.execute(
                "INSERT INTO calls VALUES ('2026-11-13 20:00:00','p','m','high',NULL,4,0,0)"
            )
            con.execute(
                "CREATE TABLE requests AS SELECT at_brt, 'a' AS account, model, "
                "200 AS status, input, output, 0.1 AS list_usd FROM calls"
            )
            con.execute(
                "CREATE TABLE tool_invocations AS SELECT at_brt, 'direct' AS source, "
                "'read' AS tool, 'requested' AS status, NULL::BOOLEAN AS error, "
                "NULL::DOUBLE AS duration_ms FROM calls"
            )
            con.execute("CREATE TABLE tasks(x INTEGER)")
            con.execute(
                "CREATE TABLE lake_watermark AS SELECT TIMESTAMP '2026-11-13 20:00:30' "
                "AS observed_through_brt"
            )
        self.receipt("a1")

    def receipt(self, run):
        value = {
            "run_id": run,
            "input_fingerprint": "fixture",
            "completed_epoch": time.time(),
            "observed_through_epoch": 1794610830,
            "fact_counts": {"calls": 1, "requests": 1, "tasks": 0, "tool_invocations": 1},
        }
        # Use the actual fixture timestamp, independent of the test runner's timezone.
        with duckdb.connect(str(self.db), read_only=True) as con:
            value["observed_through_epoch"] = con.execute(
                "SELECT epoch(observed_through_brt) + 10800 FROM lake_watermark"
            ).fetchone()[0]
            value["fact_counts"]["calls"] = con.execute("SELECT count(*) FROM calls").fetchone()[0]
        self.receipt_path = self.lake / "runtime/latest-complete.json"
        self.receipt_path.write_text(json.dumps(value))

    def test_missing_measures_and_partial_bins_are_not_zero(self):
        snapshots.publish(self.lake, self.out)
        with duckdb.connect() as con:
            row = (
                con.read_parquet(str(self.out / "a1/calls_60s/**/*.parquet"))
                .project("events,input_observed,input_tokens,bucket_closed_by_watermark")
                .fetchall()[0]
            )
        self.assertEqual(row, (1, 0, None, False))

    def test_late_arrival_rebuild_keeps_previous_snapshot(self):
        first = snapshots.publish(self.lake, self.out)
        with duckdb.connect(str(self.db)) as con:
            con.execute(
                "INSERT INTO calls SELECT at_brt - INTERVAL 1 DAY,provider,model,effort_used,"
                "input,output,cache_read,cache_write FROM calls"
            )
        self.receipt("b2")
        second = snapshots.publish(self.lake, self.out)
        self.assertEqual(second["counts"]["calls"], 2)
        self.assertEqual(json.loads((self.out / "a1/manifest.json").read_text()), first)
        # The unchanged day is shared by hard link; historical rows are not copied per generation.
        unchanged = next((self.out / "a1/calls").rglob("*.parquet"))
        relative = unchanged.relative_to(self.out / "a1")
        self.assertEqual(unchanged.stat().st_ino, (self.out / "b2" / relative).stat().st_ino)

    def test_active_writer_defers_without_publishing(self):
        self.db.with_suffix(".duckdb.wal").write_text("active")
        with self.assertRaisesRegex(RuntimeError, "writer active"):
            snapshots.publish(self.lake, self.out)
        self.assertFalse((self.out / "latest.json").exists())

    def test_changed_receipt_during_copy_is_rejected(self):
        original = snapshots.shutil.copyfile

        def changing_copy(src, dst):
            result = original(src, dst)
            self.receipt("b2")
            return result

        with patch.object(snapshots.shutil, "copyfile", changing_copy):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                snapshots.publish(self.lake, self.out)
        self.assertFalse((self.out / "latest.json").exists())

    def test_new_materialization_cannot_publish_under_previous_receipt(self):
        with duckdb.connect(str(self.db)) as con:
            con.execute("UPDATE calls SET output=99")
        with self.assertRaisesRegex(RuntimeError, "writer active"):
            snapshots.publish(self.lake, self.out)
        self.assertFalse((self.out / "latest.json").exists())

    def test_pointer_recovers_after_publish_crash(self):
        snapshots.publish(self.lake, self.out)
        (self.out / "latest.json").unlink()
        snapshots.publish(self.lake, self.out)
        self.assertEqual(json.loads((self.out / "latest.json").read_text())["run_id"], "a1")


if __name__ == "__main__":
    unittest.main()
