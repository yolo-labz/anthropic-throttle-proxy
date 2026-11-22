"""Exercise the existing offline report consumer with explicit reset metadata."""

import contextlib
import copy
import hashlib
import importlib
import io
import json
import runpy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import experiment
from reset_contract_adapter import evaluate_observations
from test_reset_contract_adapter import observation
from test_workflow import bundle


def reset_parameters(**changes):
    parameters = {
        "now": 2000.0,
        "observations": [observation()],
        "required_scopes": ["weekly_all_models"],
        "max_source_age_s": 300.0,
        "max_poll_age_s": 120.0,
        "max_clock_skew_s": 5.0,
    }
    parameters.update(changes)
    return parameters


def study(**changes):
    data = bundle()
    data["reset_metadata"] = reset_parameters(**changes)
    return data


class ResetWorkflowTests(unittest.TestCase):
    def assert_metadata_only(self, report, state):
        metadata = report["reset_metadata"]
        self.assertEqual(metadata["state"], state)
        self.assertIs(metadata["live_admission_authorized"], False)
        for key in ("capacity", "cumulative_debit", "pooled_capacity"):
            self.assertIsNone(metadata[key])
        self.assertIsNone(report["realized_savings"])

    def test_absent_option_preserves_legacy_shape(self):
        report = experiment.analyze(bundle())
        self.assertNotIn("reset_metadata", report)
        self.assertTrue(report["analysis_ready"])
        self.assertEqual(report["effect"]["point"], -30)

    def test_ready_calls_immutable_adapter_without_changing_analysis_or_input(self):
        data = study()
        before = copy.deepcopy(data)
        report = experiment.analyze(data)
        self.assert_metadata_only(report, "READY")
        self.assertEqual(report["reset_metadata"], evaluate_observations(**data["reset_metadata"]))
        del report["reset_metadata"]
        self.assertEqual(report, experiment.analyze(bundle()))
        self.assertEqual(data, before)

    def test_stale_and_cross_reset_metadata_do_not_rewrite_historical_effect(self):
        for row in (
            observation(source_observed_at=1600.0),
            observation(reset_epoch=1999.0, restoration_attestation=True),
        ):
            with self.subTest(row=row):
                report = experiment.analyze(study(observations=[row]))
                self.assert_metadata_only(report, "HOLD")
                self.assertTrue(report["analysis_ready"])
                self.assertEqual(report["effect"]["point"], -30)

    def test_empty_observations_remain_unknown(self):
        report = experiment.analyze(study(observations=[]))
        self.assert_metadata_only(report, "UNKNOWN")
        self.assertEqual(report["reset_metadata"]["reasons"], ["no_observations"])

    def test_ready_cannot_supply_missing_usage(self):
        data = study()
        data["attempts"][0]["tokens"] = None
        report = experiment.analyze(data)
        self.assert_metadata_only(report, "READY")
        self.assertFalse(report["analysis_ready"])
        self.assertIsNone(report["effect"])
        self.assertTrue(any(row["total_tokens"] is None for row in report["cohort_rows"]))
        self.assertIn("missing attempt costs", report["blockers"])

    def test_ready_cannot_supply_missing_independent_acceptance(self):
        data = study()
        data["outcomes"].pop()
        report = experiment.analyze(data)
        self.assert_metadata_only(report, "READY")
        self.assertFalse(report["analysis_ready"])
        self.assertIsNone(report["effect"])
        self.assertEqual(sum(row["unknown_success_tasks"] for row in report["cohort_rows"]), 1)

    def test_accounts_are_not_pooled(self):
        report = experiment.analyze(
            study(observations=[observation(), observation(account_id="synthetic_B")])
        )
        self.assert_metadata_only(report, "HOLD")
        self.assertIn("mixed_accounts", report["reset_metadata"]["reasons"])

    def test_envelope_rejects_null_missing_and_extra_parameters(self):
        missing = reset_parameters()
        del missing["max_poll_age_s"]
        for parameters in (None, [], "unknown", missing, reset_parameters(live_admission=True)):
            with self.subTest(parameters=parameters):
                data = bundle()
                data["reset_metadata"] = parameters
                with self.assertRaisesRegex(ValueError, "exactly the six adapter parameters"):
                    experiment.analyze(data)

    def test_contract_fields_are_not_defaulted_by_caller(self):
        row = observation()
        del row["restoration_attestation"]
        report = experiment.analyze(study(observations=[row]))
        self.assert_metadata_only(report, "HOLD")
        self.assertIn("observation_0:missing_contract_fields", report["reset_metadata"]["reasons"])


class ResetCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="reset-workflow-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.snapshot = self.root / "a1"
        self.snapshot.mkdir()
        self.file = self.snapshot / "calls.parquet"
        self.file.write_bytes(b"fixture")
        self.manifest = self.snapshot / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_id": "a1",
                    "files": {
                        "calls.parquet": {
                            "bytes": 7,
                            "sha256": hashlib.sha256(b"fixture").hexdigest(),
                        }
                    },
                }
            )
        )

    def cli_input(self, data):
        data = dict(
            data,
            snapshot_run_id="a1",
            snapshot_manifest_sha256=hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
        )
        source, output = self.root / "input.json", self.root / "report.json"
        source.write_text(json.dumps(data))
        return source, output

    def invoke(self, source, output, command=experiment.main):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return command(
                ["--input", str(source), "--snapshots", str(self.root), "--out", str(output)]
            )

    def test_cli_records_reset_result_and_exact_dependency_provenance(self):
        source, output = self.cli_input(study())
        self.assertEqual(self.invoke(source, output), 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["reset_metadata"]["state"], "READY")
        self.assertIs(report["reset_metadata"]["live_admission_authorized"], False)
        self.assertIsNone(report["realized_savings"])
        hashes = report["provenance"]["analysis_sha256"]
        self.assertEqual(
            set(hashes),
            {
                "experiment.py",
                "outcomes.py",
                "trial_stats.py",
                "science.py",
                "reset_contract_adapter.py",
                "reset_readiness_v3.py",
                "reset-observation-contract-v2.json",
            },
        )
        for name, digest in hashes.items():
            self.assertEqual(
                digest,
                hashlib.sha256(Path(experiment.__file__).with_name(name).read_bytes()).hexdigest(),
            )
        self.assertEqual(
            report["provenance"]["input_sha256"], hashlib.sha256(source.read_bytes()).hexdigest()
        )
        self.assertEqual(
            report["provenance"]["manifest_sha256"],
            hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
        )

    def test_metadata_hold_does_not_change_retrospective_exit_code(self):
        source, output = self.cli_input(study(observations=[observation(source_attested=False)]))
        self.assertEqual(self.invoke(source, output), 0)
        self.assertEqual(json.loads(output.read_text())["reset_metadata"]["state"], "HOLD")

    def test_ready_does_not_change_missing_cost_exit_code(self):
        data = study()
        data["attempts"][0]["tokens"] = None
        source, output = self.cli_input(data)
        self.assertEqual(self.invoke(source, output), 1)
        report = json.loads(output.read_text())
        self.assertEqual(report["reset_metadata"]["state"], "READY")
        self.assertIsNone(report["effect"])

    def test_ready_cannot_bypass_snapshot_checksum_gate(self):
        source, output = self.cli_input(study())
        self.file.write_bytes(b"changed")
        with self.assertRaises(SystemExit) as error:
            self.invoke(source, output)
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(output.exists())

    def test_invalid_envelope_has_no_report(self):
        data = bundle()
        data["reset_metadata"] = None
        source, output = self.cli_input(data)
        with self.assertRaises(SystemExit) as error:
            self.invoke(source, output)
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(output.exists())

    @contextlib.contextmanager
    def isolated_main(self):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            isolated = Path(temporary)
            for name in (
                "experiment.py",
                "outcomes.py",
                "trial_stats.py",
                "science.py",
                "reset_contract_adapter.py",
                "reset_readiness_v3.py",
                "reset-observation-contract-v2.json",
            ):
                shutil.copyfile(Path(experiment.__file__).with_name(name), isolated / name)
            with (
                mock.patch.dict(sys.modules),
                mock.patch.object(sys, "path", [str(isolated), *sys.path]),
            ):
                sys.modules.pop("reset_contract_adapter", None)
                namespace = runpy.run_path(str(isolated / "experiment.py"))
                yield namespace["main"], isolated

    def test_dependency_pin_mismatch_has_no_report(self):
        source, output = self.cli_input(study())
        with self.isolated_main() as (command, isolated):
            runtime = isolated / "reset_readiness_v3.py"
            runtime.write_bytes(runtime.read_bytes() + b"\n# changed dependency\n")
            with self.assertRaises(SystemExit) as error:
                self.invoke(source, output, command)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(output.exists())

    def test_cached_import_cannot_hide_drift_in_any_reset_source(self):
        source, output = self.cli_input(study())
        for name in experiment._RESET_SOURCE_PINS:
            with self.subTest(source=name), self.isolated_main() as (command, isolated):
                adapter = importlib.import_module("reset_contract_adapter")
                self.assertEqual(
                    Path(adapter.__file__).resolve(), isolated / "reset_contract_adapter.py"
                )
                first = output.with_name(name + "-before.json")
                self.assertEqual(self.invoke(source, first, command), 0)
                before = json.loads(first.read_text())
                for pinned, digest in experiment._RESET_SOURCE_PINS.items():
                    self.assertEqual(before["provenance"]["analysis_sha256"][pinned], digest)
                changed = isolated / name
                changed.write_bytes(changed.read_bytes() + b"\n# changed after cached import\n")
                with self.assertRaises(SystemExit) as error:
                    self.invoke(source, output, command)
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(output.exists())

    def test_source_drift_during_analysis_cannot_be_published_as_provenance(self):
        source, output = self.cli_input(study())
        with self.isolated_main() as (command, isolated):
            original = command.__globals__["analyze"]

            def changing(data):
                report = original(data)
                runtime = isolated / "reset_readiness_v3.py"
                runtime.write_bytes(runtime.read_bytes() + b"\n# changed during report\n")
                return report

            with mock.patch.dict(command.__globals__, {"analyze": changing}):
                with self.assertRaises(SystemExit) as error:
                    self.invoke(source, output, command)
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
