"""Offline regression controls for the exact deployed publisher and retry candidate."""

import contextlib
import fcntl
import hashlib
import io
import json
import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
import snapshots
from test_snapshots import SnapshotTests as Fixture


class StopFixture(BaseException):
    pass


def tree_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class PublisherDeferralTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.lake = self.fixture.lake
        self.out = self.fixture.out
        self.db = self.fixture.db
        snapshots.publish(self.lake, self.out)
        self.prior = tree_hashes(self.out / "a1")
        self.pointer = (self.out / "latest.json").read_bytes()
        self.fixture.receipt("b2")
        self.receipt = self.fixture.receipt_path
        self.valid = self.receipt.read_bytes()
        self.sleeps = []
        self.stdout = io.StringIO()

    def assert_prior_untouched(self):
        self.assertEqual(tree_hashes(self.out / "a1"), self.prior)

    def assert_unpublished(self):
        self.assertEqual((self.out / "latest.json").read_bytes(), self.pointer)
        self.assertFalse((self.out / "b2").exists())
        self.assertEqual(list(self.out.glob(".snapshot-*")), [])
        self.assert_prior_untouched()

    def run_loop(self, on_sleep, limit=10):
        def sleep(seconds):
            self.sleeps.append(seconds)
            on_sleep(len(self.sleeps))
            if len(self.sleeps) >= limit:
                raise StopFixture()

        env = {"LAKE_ROOT": str(self.lake), "SCIENCE_SNAPSHOTS": str(self.out)}
        with patch.dict(os.environ, env), patch("time.sleep", sleep):
            with contextlib.redirect_stdout(self.stdout):
                runpy.run_path(snapshots.__file__, run_name="__main__")

    def test_persistent_writer_survives_four_deferrals_then_publishes_closed_retry(self):
        writer = duckdb.connect(str(self.db))
        self.addCleanup(writer.close)
        writer.execute("UPDATE calls SET output=101")
        self.assertTrue(self.db.with_suffix(".duckdb.wal").exists())

        def sleep(n):
            if n <= 4:
                self.assert_unpublished()
            if n == 4:
                writer.close()
                self.assertFalse(self.db.with_suffix(".duckdb.wal").exists())
                self.fixture.receipt("c3")
            if n == 5:
                self.assertEqual(json.loads((self.out / "latest.json").read_text())["run_id"], "c3")

        with self.assertRaises(StopFixture):
            self.run_loop(sleep, limit=5)
        self.assertEqual(self.sleeps, [30] * 5)
        events = [json.loads(x) for x in self.stdout.getvalue().splitlines()]
        self.assertEqual(sum("snapshot_deferred" in x for x in events), 4)
        self.assertEqual(events[-1]["run_id"], "c3")
        self.assert_prior_untouched()
        with duckdb.connect() as con:
            values = con.read_parquet(str(self.out / "c3/calls/**/*.parquet")).project("output").fetchall()
        self.assertEqual(values, [(101,)])

    def test_real_error_still_exits_on_third_attempt(self):
        self.receipt.write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            self.run_loop(lambda n: self.assert_unpublished())
        self.assertEqual(self.sleeps, [30, 30])
        self.assert_unpublished()

    def test_deferrals_do_not_reset_the_real_error_budget(self):
        self.receipt.write_text("{")
        wal = self.db.with_suffix(".duckdb.wal")

        def sleep(n):
            self.assert_unpublished()
            if n % 2:
                self.receipt.write_bytes(self.valid)
                wal.write_text("synthetic contention marker")
            else:
                wal.unlink()
                self.receipt.write_text("{")

        with self.assertRaises(json.JSONDecodeError):
            self.run_loop(sleep)
        self.assertEqual(self.sleeps, [30] * 4)
        self.assert_unpublished()

    def test_success_resets_the_real_error_budget(self):
        self.receipt.write_text("{")

        def sleep(n):
            if n == 2:
                self.receipt.write_bytes(self.valid)
            elif n == 3:
                self.receipt.write_text("{")

        with self.assertRaises(json.JSONDecodeError):
            self.run_loop(sleep)
        self.assertEqual(self.sleeps, [30] * 5)
        self.assertEqual(json.loads((self.out / "latest.json").read_text())["run_id"], "b2")
        self.assert_prior_untouched()

    def test_mid_copy_mutation_cleans_stage_and_preserves_prior(self):
        original = snapshots.shutil.copyfile

        def changing_copy(src, dst):
            result = original(src, dst)
            value = src.stat()
            os.utime(src, ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000_000))
            return result

        with patch.object(snapshots.shutil, "copyfile", changing_copy):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                snapshots.publish(self.lake, self.out)
        self.assert_unpublished()

    def test_mid_copy_receipt_change_cleans_stage_and_preserves_prior(self):
        original = snapshots.shutil.copyfile

        def changing_copy(src, dst):
            result = original(src, dst)
            self.fixture.receipt("c3")
            return result

        with patch.object(snapshots.shutil, "copyfile", changing_copy):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                snapshots.publish(self.lake, self.out)
        self.assert_unpublished()

    def test_invalid_new_receipts_fail_closed(self):
        cases = [
            ("path", {"run_id": "../outside"}, ValueError),
            ("count", {"fact_counts": {"calls": 99}}, RuntimeError),
            ("watermark", {"observed_through_epoch": -1}, RuntimeError),
            ("table", {"fact_counts": {"not_a_fact": 1}}, ValueError),
        ]
        for name, changes, error in cases:
            with self.subTest(name=name):
                value = json.loads(self.valid)
                value.update(changes)
                self.receipt.write_text(json.dumps(value))
                with self.assertRaises(error) as result:
                    snapshots.publish(self.lake, self.out)
                if hasattr(snapshots, "SnapshotDeferred"):
                    self.assertNotIsInstance(result.exception, snapshots.SnapshotDeferred)
                self.assert_unpublished()

    def test_stale_new_receipt_defers_without_publication(self):
        value = json.loads(self.valid)
        value["completed_epoch"] = self.db.stat().st_mtime - 1
        self.receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, "writer active"):
            snapshots.publish(self.lake, self.out)
        self.assert_unpublished()

    def test_pointer_failure_preserves_old_pointer_and_recovers_atomically(self):
        replace = snapshots.os.replace
        attempts = []

        def fail_latest(src, dst):
            if Path(dst) == self.out / "latest.json":
                attempts.append(1)
                self.assertEqual((self.out / "latest.json").read_bytes(), self.pointer)
                self.assertTrue((self.out / "b2/manifest.json").is_file())
                raise OSError("injected atomic pointer failure")
            return replace(src, dst)

        with patch.object(snapshots.os, "replace", fail_latest):
            with self.assertRaisesRegex(OSError, "injected"):
                snapshots.publish(self.lake, self.out)
        self.assertEqual(attempts, [1])
        committed = tree_hashes(self.out / "b2")
        result = snapshots.publish(self.lake, self.out)
        self.assertEqual(result["run_id"], "b2")
        self.assertEqual(json.loads((self.out / "latest.json").read_bytes()), result)
        self.assertEqual(tree_hashes(self.out / "b2"), committed)
        self.assert_prior_untouched()

    def test_output_lock_remains_exclusive(self):
        with (self.out / ".publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                snapshots.publish(self.lake, self.out)
            self.assert_unpublished()
        self.assertEqual(snapshots.publish(self.lake, self.out)["run_id"], "b2")

    def test_process_interruption_is_not_swallowed(self):
        self.db.with_suffix(".duckdb.wal").write_text("synthetic contention marker")

        def interrupt(n):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.run_loop(interrupt)
        self.assertEqual(self.sleeps, [30])
        self.assert_unpublished()


if __name__ == "__main__":
    unittest.main()
