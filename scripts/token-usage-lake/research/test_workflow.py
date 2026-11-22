"""Offline regression checks for the canonical research path."""

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import experiment
import science
from trial_stats import paired_cluster_bootstrap, quality_gate


class SnapshotIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "a1"
        self.path.mkdir()
        self.file = self.path / "calls/data.parquet"
        self.file.parent.mkdir()
        self.file.write_bytes(b"fixture")
        self.manifest = {
            "version": 1,
            "run_id": "a1",
            "files": {
                "calls/data.parquet": {"bytes": 7, "sha256": hashlib.sha256(b"fixture").hexdigest()}
            },
        }
        self.save()

    def save(self):
        text = json.dumps(self.manifest)
        (self.path / "manifest.json").write_text(text)
        (self.root / "latest.json").write_text(text)

    def test_valid_snapshot(self):
        path, manifest = science.latest_snapshot(self.root)
        self.assertEqual((path, manifest), (self.path, self.manifest))

    def cli(self, data, name):
        data = dict(
            data,
            snapshot_run_id="a1",
            snapshot_manifest_sha256=hashlib.sha256(
                (self.path / "manifest.json").read_bytes()
            ).hexdigest(),
        )
        source, output = self.root / (name + "-input.json"), self.root / (name + "-report.json")
        source.write_text(json.dumps(data))
        with contextlib.redirect_stdout(io.StringIO()):
            status = experiment.main(
                ["--input", str(source), "--snapshots", str(self.root), "--out", str(output)]
            )
        return status, json.loads(output.read_text())

    def test_cli_pins_and_records_input_code_and_dataset(self):
        status, report = self.cli(bundle(), "complete")
        self.assertEqual(status, 0)
        self.assertEqual(report["provenance"]["run_id"], "a1")
        self.assertEqual(len(report["provenance"]["analysis_sha256"]), 4)
        self.assertIsNone(report["realized_savings"])

    def test_cli_incomplete_data_exits_nonzero_with_unknown_effect(self):
        data = bundle()
        data["outcomes"].pop()
        status, report = self.cli(data, "incomplete")
        self.assertEqual(status, 1)
        self.assertIsNone(report["effect"])
        self.assertTrue(report["blockers"])

    def test_changed_file_is_rejected(self):
        self.file.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            science.latest_snapshot(self.root)

    def test_missing_file_is_rejected(self):
        self.file.unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            science.latest_snapshot(self.root)

    def test_latest_cannot_escape_root(self):
        for run_id in ("../outside", "", None, 42):
            with self.subTest(run_id=run_id):
                (self.root / "latest.json").write_text(json.dumps({"run_id": run_id}))
                with self.assertRaises(ValueError):
                    science.latest_snapshot(self.root)

    def test_pointer_and_immutable_manifest_must_agree(self):
        pointer = copy.deepcopy(self.manifest)
        pointer["files"] = {}
        (self.root / "latest.json").write_text(json.dumps(pointer))
        with self.assertRaisesRegex(ValueError, "manifest"):
            science.latest_snapshot(self.root)

    def test_file_path_cannot_escape_snapshot(self):
        self.manifest["files"] = {"../outside.parquet": {"bytes": 1, "sha256": "x"}}
        self.save()
        with self.assertRaises(ValueError):
            science.latest_snapshot(self.root)

    def test_symlink_file_is_rejected(self):
        outside = self.root / "outside.parquet"
        outside.write_bytes(b"fixture")
        self.file.unlink()
        self.file.symlink_to(outside)
        with self.assertRaises(ValueError):
            science.latest_snapshot(self.root)


def bundle():
    attempts, outcomes = [], []
    for session in range(4):
        for arm, cost in (("control", 100), ("treatment", 70)):
            task = f"{session}-{arm}"
            attempts.append(
                {
                    "attempt_key": task,
                    "task_key": task,
                    "project_key": "fixture",
                    "session_hash": f"session{session}",
                    "task_start_epoch": 100,
                    "attempt_epoch": 101,
                    "arm": arm,
                    "points_unit": "fixture_points",
                    "calibration_id": "fixture-v1",
                    "tokens": cost,
                    "points": cost / 10,
                    "status": "ok",
                }
            )
            outcomes.append(
                {
                    "task_key": task,
                    "grade": "accepted_task",
                    "oracle_kind": "declared_oracle",
                    "oracle_provenance_verified": True,
                    "accepted": True,
                    "accepted_epoch": 102,
                }
            )
    return {
        "usage_grain": "native",
        "assignment_unit": "paired_session",
        "freeze_epoch": 0,
        "coverage_complete": True,
        "attempts": attempts,
        "outcomes": outcomes,
    }


class OutcomeWorkflowTests(unittest.TestCase):
    def test_complete_pairs_compute_exploratory_effect_not_savings(self):
        report = experiment.analyze(bundle())
        self.assertTrue(report["analysis_ready"])
        self.assertEqual(report["effect"]["point"], -30)
        self.assertEqual(report["effect"]["interval90"], [-30, -30])
        self.assertIsNone(report["realized_savings"])
        self.assertFalse(report["quality"]["pass_gate"])  # Tiny sample is not equal-quality proof.

    def test_unknown_outcome_blocks_not_recoded_as_failure(self):
        data = bundle()
        data["outcomes"].pop()
        report = experiment.analyze(data)
        self.assertFalse(report["analysis_ready"])
        self.assertIsNone(report["effect"])
        self.assertEqual(sum(r["unknown_success_tasks"] for r in report["cohort_rows"]), 1)

    def test_missing_cost_is_not_zero(self):
        data = bundle()
        data["attempts"][0]["tokens"] = None
        self.assertFalse(experiment.analyze(data)["analysis_ready"])

    def test_all_failed_and_rework_spend_remains(self):
        data = bundle()
        for status in ("failed", "rework", "aborted"):
            row = dict(data["attempts"][1], attempt_key=status, status=status, tokens=40)
            data["attempts"].append(row)
        report = experiment.analyze(data)
        self.assertTrue(report["analysis_ready"])
        self.assertEqual(report["effect"]["point"], 0)  # 3 * 40 / 4 consumed the apparent gain.

    def test_duplicate_attempt_is_rejected(self):
        data = bundle()
        data["attempts"].append(dict(data["attempts"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate attempt"):
            experiment.analyze(data)

    def test_incomplete_coverage_blocks(self):
        data = bundle()
        data["coverage_complete"] = False
        self.assertFalse(experiment.analyze(data)["analysis_ready"])

    def test_unpaired_session_is_not_dropped(self):
        data = bundle()
        data["attempts"][0]["session_hash"] = "unpaired"
        self.assertFalse(experiment.analyze(data)["analysis_ready"])

    def test_resource_strata_are_not_pooled(self):
        data = bundle()
        data["attempts"][0]["points_unit"] = "other_resource"
        self.assertFalse(experiment.analyze(data)["analysis_ready"])

    def test_unverified_acceptance_is_refused(self):
        data = bundle()
        data["outcomes"][0]["oracle_provenance_verified"] = False
        with self.assertRaisesRegex(ValueError, "unverified/self acceptance"):
            experiment.analyze(data)

    def test_artifact_is_not_an_accepted_task(self):
        data = bundle()
        data["outcomes"][0] = {
            "task_key": "0-control",
            "grade": "artifact_proxy",
            "dedup_complete": True,
            "work_unit_key": "PR1",
            "accepted_epoch": 102,
        }
        self.assertFalse(experiment.analyze(data)["analysis_ready"])

    def test_mixed_source_grain_is_rejected(self):
        data = bundle()
        data["usage_grain"] = "native+gateway"
        with self.assertRaisesRegex(ValueError, "one usage grain"):
            experiment.analyze(data)

    def test_zero_acceptance_is_not_a_zero_cost_ratio(self):
        data = bundle()
        for outcome in data["outcomes"]:
            outcome["accepted"] = False
        self.assertFalse(experiment.analyze(data)["analysis_ready"])

    def test_statistics_are_paired_at_session_not_call_level(self):
        rows = [
            {
                "control_accepted": 1,
                "treatment_accepted": 1,
                "control_tokens": n,
                "treatment_tokens": n,
            }
            for n in range(1, 5)
        ]
        self.assertEqual(paired_cluster_bootstrap(rows, draws=100)["interval90"], [0, 0])
        self.assertFalse(quality_gate((3, 3), (3, 3))["pass_gate"])

    def test_only_manifest_files_are_selected(self):
        paths = science.parquet_paths(
            Path("snapshot"),
            {
                "files": {
                    "calls/a.parquet": {},
                    "calls_extra/b.parquet": {},
                    "calls_60s/c.parquet": {},
                }
            },
            "calls",
        )
        self.assertEqual(paths, [str(Path("snapshot/calls/a.parquet"))])


if __name__ == "__main__":
    unittest.main()
