"""Exercise cached-receipt safeguards through actual DuckDB publisher entrypoints."""

import hashlib
import json
import os
import unittest
from unittest.mock import patch

import snapshots
from test_snapshots import SnapshotTests as Fixture


class CachedPublishTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.lake, self.out = self.fixture.lake, self.fixture.out
        self.receipt = self.fixture.receipt_path
        self.old_receipt = self.receipt.read_bytes()
        snapshots.publish(self.lake, self.out)
        self.fixture.receipt("b2")
        snapshots.publish(self.lake, self.out)
        self.current_receipt = self.receipt.read_bytes()
        self.pointer = (self.out / "latest.json").read_bytes()
        self.immutable = self.hash_generations()

    def hash_generations(self):
        return {
            str(path.relative_to(self.out)): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in ("a1", "b2")
            for path in (self.out / name).rglob("*")
            if path.is_file()
        }

    def assert_prior_preserved(self):
        self.assertEqual((self.out / "latest.json").read_bytes(), self.pointer)
        self.assertEqual(self.hash_generations(), self.immutable)
        self.assertEqual(list(self.out.glob(".snapshot-*")), [])

    def test_incomplete_cached_receipt_preserves_latest(self):
        self.receipt.write_text('{"run_id":"a1"}')
        with self.assertRaises(ValueError):
            snapshots.publish(self.lake, self.out)
        self.assert_prior_preserved()

    def test_older_valid_cached_receipt_preserves_latest(self):
        self.receipt.write_bytes(self.old_receipt)
        with self.assertRaisesRegex(ValueError, "backwards"):
            snapshots.publish(self.lake, self.out)
        self.assert_prior_preserved()

    def test_reused_generation_rejects_changed_receipt_bindings(self):
        changes = {
            "input_fingerprint": "other",
            "fact_counts": {"calls": 99},
            "observed_through_epoch": 1.0,
            "completed_epoch": 1.0,
            "data_quality": {"source_freshness_1200": True},
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                receipt = json.loads(self.current_receipt)
                receipt[key] = value
                self.receipt.write_text(json.dumps(receipt))
                with self.assertRaises(ValueError):
                    snapshots.publish(self.lake, self.out)
                self.assert_prior_preserved()

    def test_valid_cached_retry_uses_only_immutable_generation(self):
        with patch.object(snapshots, "file_identity", side_effect=AssertionError("live lake stat")):
            with patch.object(snapshots.duckdb, "connect", side_effect=AssertionError("DuckDB open")):
                result = snapshots.publish(self.lake, self.out)
        self.assertEqual(result["run_id"], "b2")
        self.assert_prior_preserved()

    def test_new_generation_path_cannot_regress_known_latest(self):
        receipt = json.loads(self.current_receipt)
        receipt["run_id"] = "c3"
        receipt["completed_epoch"] -= 5
        self.receipt.write_text(json.dumps(receipt))
        earlier = receipt["completed_epoch"] - 1
        os.utime(self.fixture.db, (earlier, earlier))
        with self.assertRaisesRegex(ValueError, "backwards"):
            snapshots.publish(self.lake, self.out)
        self.assert_prior_preserved()
        # A complete immutable directory may remain after pointer refusal; it is not published.
        self.assertTrue((self.out / "c3/manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
