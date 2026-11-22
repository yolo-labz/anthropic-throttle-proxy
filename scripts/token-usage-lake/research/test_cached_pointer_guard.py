"""Pure-file controls for the separate cache guard proposal; no lake or DB."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import snapshots as guard


class CachedPointerGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.ra = self.make("a1", 100.0)
        self.b, self.rb = self.make("b2", 200.0)
        guard.atomic_json(self.root / "latest.json", self.b)
        self.prior = (self.root / "latest.json").read_bytes()
    def make(self, run, epoch):
        manifest = {"version": 1, "run_id": run, "completed_epoch": epoch,
                    "observed_through_epoch": epoch - 1, "source_fingerprint": "fixture",
                    "counts": {"calls": 1, "requests": 1, "tasks": 0, "tool_invocations": 1},
                    "data_quality": {"source_freshness_1200": "not_recorded"}, "files": {}}
        path = self.root / run
        path.mkdir()
        guard.atomic_json(path / "manifest.json", manifest)
        receipt = {"run_id": run, "completed_epoch": epoch, "observed_through_epoch": epoch - 1,
                   "input_fingerprint": "fixture", "fact_counts": manifest["counts"].copy()}
        return manifest, receipt
    def rejected(self, receipt, manifest):
        with self.assertRaises((ValueError, OSError)):
            guard.promote_manifest(self.root, receipt, manifest)
        self.assertEqual((self.root / "latest.json").read_bytes(), self.prior)
    def test_invalid_minimal_cached_receipt(self):
        self.rejected({"run_id": "a1"}, self.a)
    def test_old_full_valid_cached_receipt(self):
        self.rejected(self.ra, self.a)
    def test_all_required_receipt_bindings(self):
        for key, value in (
            ("run_id", "c3"),
            ("completed_epoch", 201),
            ("observed_through_epoch", 201),
            ("input_fingerprint", "other"),
            ("fact_counts", {"calls": 99}),
            ("data_quality", {"source_freshness_1200": "changed"}),
        ):
            with self.subTest(key=key):
                receipt = copy.deepcopy(self.rb)
                receipt[key] = value
                self.rejected(receipt, self.b)
    def test_version_and_numeric_types(self):
        for key, value in (
            ("version", True),
            ("version", 2),
            ("completed_epoch", True),
            ("completed_epoch", float("nan")),
            ("observed_through_epoch", float("inf")),
        ):
            with self.subTest(key=key):
                manifest = copy.deepcopy(self.b)
                manifest[key] = value
                self.rejected(self.rb, manifest)
    def test_equal_time_different_run_is_ambiguous(self):
        manifest, receipt = self.make("c3", 200.0)
        self.rejected(receipt, manifest)
    def test_same_generation_retry_is_idempotent(self):
        guard.promote_manifest(self.root, self.rb, self.b)
        self.assertEqual((self.root / "latest.json").read_bytes(), self.prior)
    def test_renamed_forward_generation_recovers_pointer(self):
        manifest, receipt = self.make("c3", 300.0)
        immutable = (self.root / "c3/manifest.json").read_bytes()
        guard.promote_manifest(self.root, receipt, manifest)
        self.assertEqual(json.loads((self.root / "latest.json").read_bytes()), manifest)
        self.assertEqual((self.root / "c3/manifest.json").read_bytes(), immutable)
    def test_missing_latest_recovers_cached_generation(self):
        (self.root / "latest.json").unlink()
        guard.promote_manifest(self.root, self.rb, self.b)
        self.assertEqual((self.root / "latest.json").read_bytes(), self.prior)
    def test_divergent_latest_pointer_is_preserved(self):
        changed = copy.deepcopy(self.b)
        changed["counts"]["calls"] = 9
        guard.atomic_json(self.root / "latest.json", changed)
        self.prior = (self.root / "latest.json").read_bytes()
        self.rejected(self.rb, self.b)
    def test_missing_latest_generation_is_preserved(self):
        (self.root / "b2/manifest.json").unlink()
        self.rejected(self.rb, self.b)
    def test_nonrequired_new_receipt_fields_remain_allowed(self):
        receipt = copy.deepcopy(self.rb)
        receipt.update({"version": 17, "accepted": True, "kind": "fixture"})
        guard.promote_manifest(self.root, receipt, self.b)
        self.assertEqual((self.root / "latest.json").read_bytes(), self.prior)
    def test_quality_distinguishes_null_false_zero(self):
        for quality in (None, False, 0, {}):
            with self.subTest(quality=quality):
                receipt = copy.deepcopy(self.rb)
                receipt["data_quality"] = quality
                self.rejected(receipt, self.b)

if __name__ == "__main__":
    unittest.main(verbosity=2)
