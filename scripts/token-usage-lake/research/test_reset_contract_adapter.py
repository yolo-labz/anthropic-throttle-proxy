"""Synthetic contract-boundary regressions; no provider or production access."""

import copy
import hashlib
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import reset_contract_adapter as adapter


def observation(**changes):
    row = {
        "account_id": "synthetic_A",
        "limit_scope_id": "weekly_all_models",
        "poll_time": 1999.0,
        "source_observed_at": 1998.0,
        "source_clock_type": "local_proxy_wall_clock_at_header_application",
        "source_clock_provenance": "synthetic_header_application_clock",
        "reset_epoch": 2100.0,
        "utilization_fraction": 0.25,
        "duplicate_sample": False,
        "producer_version": "synthetic_v1",
        "source_timestamp_identity": "synthetic_source_sample_1998",
        "source_attested": True,
        "source_evidence_id": "synthetic_source_evidence_1",
        "quantization": 0.01,
        "clock_skew_bound_seconds": 1.0,
        "restoration_attestation": None,
        "derived_display_projection": False,
    }
    row.update(changes)
    return row


def evaluate(rows, **changes):
    parameters = {
        "now": 2000.0,
        "observations": rows,
        "required_scopes": ["weekly_all_models"],
        "max_source_age_s": 300.0,
        "max_poll_age_s": 120.0,
        "max_clock_skew_s": 5.0,
    }
    parameters.update(changes)
    return adapter.evaluate_observations(**parameters)


class ResetContractAdapterTests(unittest.TestCase):
    def assert_metadata_result(self, result, state):
        self.assertEqual(result["state"], state)
        for key in ("capacity", "cumulative_debit", "pooled_capacity"):
            self.assertIsNone(result[key])
        self.assertIs(result["live_admission_authorized"], False)

    def test_exact_contract_maps_all_four_aliases(self):
        row = observation(restoration_attestation=True)
        # The existing schema is incompatible with direct v3 construction.
        with self.assertRaises(TypeError):
            adapter._READINESS.Bucket(**row)
        normalized = {adapter._RENAMES.get(k, k): v for k, v in row.items()}
        expected = adapter._READINESS.readiness(
            now=2000.0,
            buckets=[adapter._READINESS.Bucket(**normalized)],
            required_scopes=["weekly_all_models"],
            max_source_age_s=300.0,
            max_poll_age_s=120.0,
            max_clock_skew_s=5.0,
        )
        actual = evaluate([row])
        self.assert_metadata_result(actual, "READY")
        for key, value in expected.items():
            self.assertEqual(actual[key], value)

    def test_original_input_records_not_modified(self):
        rows = [observation()]
        before = copy.deepcopy(rows)
        evaluate(rows)
        self.assertEqual(rows, before)

    def test_missing_contract_field_is_not_defaulted(self):
        row = observation()
        del row["restoration_attestation"]
        self.assert_metadata_result(evaluate([row]), "HOLD")

    def test_runtime_aliases_and_unknown_payload_refused(self):
        for key, value in (
            ("attested", True),
            ("scope_id", "weekly_all_models"),
            ("clock_skew_bound_s", 0),
            ("restoration_attested", True),
            ("raw", "unapproved_content"),
        ):
            with self.subTest(key=key):
                self.assert_metadata_result(evaluate([observation(**{key: value})]), "HOLD")

    def test_empty_observations_remain_unknown(self):
        self.assert_metadata_result(evaluate([]), "UNKNOWN")

    def test_malformed_records_fail_closed(self):
        for rows in (None, "text", {}, [None], [1], [[observation()]]):
            with self.subTest(rows=rows):
                self.assert_metadata_result(evaluate(rows), "HOLD")

    def test_duplicate_scope_not_collapsed(self):
        self.assert_metadata_result(
            evaluate([observation(source_attested=False), observation()]), "HOLD"
        )

    def test_mixed_accounts_not_pooled(self):
        self.assert_metadata_result(
            evaluate([observation(), observation(account_id="synthetic_B")]), "HOLD"
        )

    def test_missing_required_scope_refused(self):
        self.assert_metadata_result(
            evaluate([observation()], required_scopes=["weekly_all_models", "session_5h"]), "HOLD"
        )

    def test_recent_duplicate_retains_metadata_readiness(self):
        self.assert_metadata_result(evaluate([observation(duplicate_sample=True)]), "READY")

    def test_old_source_not_refreshed_by_new_poll(self):
        self.assert_metadata_result(
            evaluate([observation(source_observed_at=1600.0, poll_time=2000.0)]), "HOLD"
        )

    def test_missing_flag_does_not_mean_fresh(self):
        self.assert_metadata_result(
            evaluate([observation(duplicate_sample=None, producer_version=None)]), "HOLD"
        )

    def test_pre_reset_observation_stays_held(self):
        self.assert_metadata_result(
            evaluate([observation(reset_epoch=1999.0, restoration_attestation=True)]), "HOLD"
        )

    def test_restoration_mapping_is_not_assumed(self):
        for restored, expected in ((None, "HOLD"), (False, "HOLD"), (True, "READY")):
            with self.subTest(restored=restored):
                self.assert_metadata_result(
                    evaluate([observation(reset_epoch=1995.0, restoration_attestation=restored)]),
                    expected,
                )

    def test_display_zero_never_becomes_measurement(self):
        self.assert_metadata_result(
            evaluate([observation(utilization_fraction=0.0, derived_display_projection=True)]),
            "HOLD",
        )

    def test_untrusted_assertion_and_string_boolean_refused(self):
        for attested in (False, "true", 1, None):
            with self.subTest(attested=attested):
                self.assert_metadata_result(
                    evaluate([observation(source_attested=attested)]), "HOLD"
                )

    def test_unknown_utilization_not_coerced_to_zero(self):
        self.assert_metadata_result(evaluate([observation(utilization_fraction=None)]), "HOLD")

    def test_invalid_clock_thresholds_and_counters_refused(self):
        for changes in (
            {"clock_skew_bound_seconds": 6.0},
            {"source_observed_at": float("nan")},
            {"poll_time": "1999"},
            {"utilization_fraction": True},
        ):
            with self.subTest(changes=changes):
                self.assert_metadata_result(evaluate([observation(**changes)]), "HOLD")
        self.assert_metadata_result(
            evaluate([observation()], max_source_age_s=float("nan")), "HOLD"
        )

    def test_contract_and_runtime_bytes_match_existing_sources(self):
        root = Path(__file__).resolve().parent
        for name, digest in adapter._PINS.items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)

    def test_changed_runtime_refuses_import(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="tu02r-adapter-pin-") as tmp:
            target = Path(tmp)
            for name in ("reset_contract_adapter.py", *adapter._PINS):
                shutil.copyfile(root / name, target / name)
            source = target / "reset_readiness_v3.py"
            source.write_bytes(source.read_bytes() + b"\n# changed dependency\n")
            result = subprocess.run(
                [sys.executable, "-B", "-c", "import reset_contract_adapter"],
                cwd=target,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reset_adapter_dependency_pin_mismatch", result.stderr)

    def test_unverified_bytecode_is_never_loaded_or_written(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="tu02r-unverified-pyc-") as tmp:
            target = Path(tmp)
            runtime = target / "reset_readiness_v3.py"
            runtime.write_text("raise AssertionError('unverified bytecode executed')\n")
            cache = Path(
                py_compile.compile(
                    str(runtime),
                    doraise=True,
                    invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
                )
            )
            cache_before = cache.read_bytes()
            for name in adapter._PINS:
                shutil.copyfile(root / name, target / name)
            with mock.patch.object(adapter, "_ROOT", target), mock.patch.dict(sys.modules):
                fields, module = adapter._load_inputs()
                self.assertEqual(fields, adapter._FIELDS)
                self.assertEqual(module.State.READY.value, "READY")
                loader = module.__loader__
                with self.assertRaises(OSError):
                    loader.get_data(str(cache))
                with self.assertRaises(OSError):
                    loader.get_data(str(target / "other.py"))
                with self.assertRaises(OSError):
                    loader.set_data(str(cache), b"replacement")
            self.assertEqual(cache.read_bytes(), cache_before)

    def test_source_replacement_after_verification_cannot_change_executed_bytes(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="tu02r-verified-buffer-") as tmp:
            target = Path(tmp)
            for name in adapter._PINS:
                shutil.copyfile(root / name, target / name)
            runtime = target / "reset_readiness_v3.py"
            original_read_bytes = Path.read_bytes
            replacement = "raise AssertionError('replacement source executed')\n"

            def read_then_replace(path):
                content = original_read_bytes(path)
                if path == runtime:
                    path.write_text(replacement)
                return content

            with (
                mock.patch.object(adapter, "_ROOT", target),
                mock.patch.dict(sys.modules),
                mock.patch.object(Path, "read_bytes", read_then_replace),
            ):
                fields, module = adapter._load_inputs()
                self.assertEqual(fields, adapter._FIELDS)
                self.assertEqual(module.State.READY.value, "READY")
            self.assertEqual(runtime.read_text(), replacement)


if __name__ == "__main__":
    unittest.main()
