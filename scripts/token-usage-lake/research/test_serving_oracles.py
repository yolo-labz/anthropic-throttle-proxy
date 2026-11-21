"""Read-only serving and mechanical oracle layers, verified offline."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb
import oracles
import science
import serving


def build_generation(root, run_id="a1"):
    """One declared calls table, one declared burn table, one undeclared decoy file."""
    root = Path(root)
    generation = root / run_id
    generation.mkdir(parents=True)
    seed = duckdb.connect(":memory:")
    seed.execute("CREATE TABLE calls (bucket_epoch BIGINT, provider TEXT, value DOUBLE)")
    seed.execute(
        "INSERT INTO calls VALUES (100, 'zai', 1.0), (200, 'zai', 2.0), (300, 'zai', 3.0),"
        " (400, 'openai', 4.0), (500, 'openai', NULL), (600, 'openai', 9.0)"
    )
    seed.execute("COPY calls TO ? (FORMAT parquet)", [str(generation / "calls.parquet")])
    seed.execute("CREATE TABLE burn (bucket_epoch BIGINT, points DOUBLE)")
    seed.execute("INSERT INTO burn VALUES (100, 0.5)")
    seed.execute("COPY burn TO ? (FORMAT parquet)", [str(generation / "burn.parquet")])
    seed.close()
    seed.close()
    files = {}
    for name in ("burn.parquet", "calls.parquet"):
        path = generation / name
        files[name] = {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    manifest = {"version": 1, "run_id": run_id, "files": files}
    (generation / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    duckdb.connect(":memory:").execute(
        "COPY (SELECT 1) TO ? (FORMAT parquet)", [str(generation / "decoy.parquet")]
    )
    (root / "latest.json").write_text(json.dumps(manifest, sort_keys=True))
    return root, generation


class ServingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root, self.generation = build_generation(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_deep_verification_lists_declared_tables_only(self):
        receipt = serving.verify(self.root, deep=True, epoch=1)
        self.assertEqual(receipt["tables"], ["burn", "calls"])
        self.assertEqual(receipt["mode"], "deep")
        self.assertEqual(set(receipt["files"]), {"burn.parquet", "calls.parquet"})

    def test_matching_receipt_skips_hashing(self):
        serving.verify(self.root, deep=True, epoch=1)
        with mock.patch.object(science, "verify_files", side_effect=AssertionError("hashed")):
            receipt = serving.verify(self.root)
        self.assertEqual(receipt["mode"], "cached")

    def test_hostile_pointer_is_refused_before_hashing(self):
        serving.verify(self.root, deep=True, epoch=1)
        manifest = json.loads((self.root / "latest.json").read_text())
        manifest["run_id"] = "b2"
        (self.root / "latest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            serving.verify(self.root)

    def test_changed_file_forces_reverification_and_refuses(self):
        serving.verify(self.root, deep=True, epoch=1)
        (self.generation / "calls.parquet").write_bytes(b"not a parquet file")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            serving.verify(self.root)

    def test_forged_manifest_and_pointer_still_fail_the_checksum(self):
        serving.verify(self.root, deep=True, epoch=1)
        manifest = json.loads((self.generation / "manifest.json").read_text())
        manifest["files"]["calls.parquet"]["bytes"] += 1
        forged = json.dumps(manifest)
        (self.generation / "manifest.json").write_text(forged)
        (self.root / "latest.json").write_text(forged)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            serving.verify(self.root)

    def test_open_exposes_views_without_undeclared_files(self):
        connection, receipt = serving.open_verified(self.root, deep=True, epoch=1)
        self.assertEqual(receipt["run_id"], "a1")
        self.assertEqual(connection.execute("SELECT count(*) FROM calls").fetchone()[0], 6)
        self.assertNotIn("decoy", [row[0] for row in connection.execute("SHOW TABLES").fetchall()])
        with self.assertRaises(duckdb.Error):
            connection.execute("INSERT INTO calls VALUES (1, 'x', 1.0)")
        connection.close()

    def test_unknown_table_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not in verified generation"):
            serving.open_verified(self.root, deep=True, tables=["decoy"], epoch=1)

    def test_in_place_rewrite_with_restored_mtime_invalidates_the_cache(self):
        serving.verify(self.root, deep=True, epoch=1)
        path = self.generation / "calls.parquet"
        before = path.stat()
        original = path.read_bytes()
        path.write_bytes(b"X" + original[1:])  # same size, different content
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # restore mtime only
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            serving.verify(self.root)

    def test_forged_receipt_is_not_reused(self):
        serving.verify(self.root, deep=True, epoch=1)
        receipt_path = self.root / ".verify" / "a1.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["verified_epoch"] = 999999
        receipt_path.write_text(json.dumps(receipt))
        # The chain has no entry for these bytes, so cheap reuse is refused and the deep path runs.
        self.assertEqual(serving.verify(self.root)["mode"], "deep")

    def test_corrupt_chain_is_refused(self):
        serving.verify(self.root, deep=True, epoch=1)
        chain = self.root / ".verify" / "chain.jsonl"
        lines = chain.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["run_id"] = "tampered"
        chain.write_text(json.dumps(entry) + "\n")
        with self.assertRaisesRegex(ValueError, "verification chain is corrupt"):
            serving.verify(self.root)

    def test_receipt_lives_outside_the_generation(self):
        serving.verify(self.root, deep=True, epoch=1)
        self.assertTrue((self.root / ".verify" / "a1.json").exists())
        self.assertEqual(
            sorted(path.name for path in self.generation.iterdir()),
            ["burn.parquet", "calls.parquet", "decoy.parquet", "manifest.json"],
        )


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root, _generation = build_generation(self.temporary.name)
        self.connection, _receipt = serving.open_verified(self.root, deep=True, epoch=1)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_digest_is_order_insensitive_and_stable(self):
        first = oracles.digest(self.connection, "SELECT * FROM calls")
        second = oracles.digest(self.connection, "SELECT * FROM calls ORDER BY value DESC")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["rows"], 6)
        self.assertNotEqual(
            first["sha256"],
            oracles.digest(self.connection, "SELECT * FROM calls LIMIT 5")["sha256"],
        )

    def test_replay_reports_pass_fail_and_absence_of_evidence(self):
        sql = "SELECT provider, count(*) FROM calls GROUP BY 1"
        recorded = oracles.digest(self.connection, sql)["sha256"]
        self.assertEqual(oracles.replay(self.connection, sql, recorded)["status"], "pass")
        self.assertEqual(oracles.replay(self.connection, sql, "0" * 64)["status"], "fail")
        self.assertEqual(oracles.replay(self.connection, sql)["status"], "not_applicable")

    def test_invariants_pass_fail_and_empty_set(self):
        passing = oracles.invariants(
            self.connection,
            [
                {"kind": "row_count_between", "table": "calls", "min": 1, "max": 10},
                {
                    "kind": "accepted_values",
                    "table": "calls",
                    "column": "provider",
                    "values": ["zai", "openai"],
                },
                {"kind": "custom_sql", "table": "calls", "predicate": "value > 0 OR value IS NULL"},
            ],
        )
        self.assertEqual(passing["status"], "pass")
        failing = oracles.invariants(
            self.connection,
            [
                {"kind": "not_null", "table": "calls", "column": "value"},
                {"kind": "unique", "table": "calls", "column": "provider"},
            ],
        )
        self.assertEqual(failing["status"], "fail")
        self.assertEqual([rule["passed"] for rule in failing["rules"]], [False, False])
        self.assertEqual(oracles.invariants(self.connection, [])["status"], "not_applicable")

    def test_invariants_refuse_unsafe_identifiers(self):
        with self.assertRaisesRegex(ValueError, "unsafe identifier"):
            oracles.invariants(
                self.connection,
                [{"kind": "not_null", "table": "calls; DROP TABLE calls", "column": "value"}],
            )

    def backtest_spec(self):
        return {
            "table": "calls",
            "time_column": "bucket_epoch",
            "validation": [0, 400],
            "holdout": [400, 700],
            "select": "max",
            "trial_count": 1,
            "candidates": [
                {"name": "mean", "expression": "avg(value)"},
                {"name": "best", "expression": "max(value)"},
            ],
        }

    def test_backtest_selects_on_validation_and_reads_holdout_once(self):
        receipt = Path(self.temporary.name) / "holdout.json"
        result = oracles.backtest(self.connection, self.backtest_spec(), receipt, epoch=2)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["chosen"], "best")
        self.assertEqual(result["validation"], 3.0)
        self.assertEqual(result["holdout"], 9.0)
        self.assertTrue(json.loads(receipt.read_text())["holdout_used_once"])
        with self.assertRaisesRegex(RuntimeError, "holdout already used"):
            oracles.backtest(self.connection, self.backtest_spec(), receipt)

    def test_second_distinct_specification_must_raise_the_trial_count(self):
        receipt = Path(self.temporary.name) / "holdout.json"
        oracles.backtest(self.connection, self.backtest_spec(), receipt, epoch=2)
        second = self.backtest_spec()
        second["candidates"] = [{"name": "solo", "expression": "min(value)"}]
        with self.assertRaisesRegex(ValueError, "trial ledger records 1 prior trial"):
            oracles.backtest(self.connection, second, receipt, epoch=2)
        second["trial_count"] = 2
        result = oracles.backtest(self.connection, second, receipt, epoch=2)
        self.assertEqual(result["multiplicity"], "unadjusted")
        declared = self.backtest_spec()
        declared["candidates"] = [{"name": "solo", "expression": "min(value)"}]
        declared["trial_count"] = 3
        declared["adjustment"] = "holm-bonferroni"
        self.assertEqual(
            oracles.backtest(self.connection, declared, receipt, epoch=2)["multiplicity"],
            "holm-bonferroni",
        )

    def test_trial_count_must_be_declared(self):
        spec = self.backtest_spec()
        del spec["trial_count"]
        with self.assertRaisesRegex(ValueError, "declare trial_count"):
            oracles.backtest(self.connection, spec, Path(self.temporary.name) / "y.json")

    def test_backtest_refuses_overlapping_windows(self):
        spec = self.backtest_spec()
        spec["validation"] = [0, 500]
        spec["holdout"] = [400, 700]
        with self.assertRaisesRegex(ValueError, "disjoint holdout"):
            oracles.backtest(self.connection, spec, Path(self.temporary.name) / "x.json")

    def test_verdict_never_inflates_coverage(self):
        nothing = oracles.verdict([{"layer": "solver", "status": "not_applicable"}])
        self.assertFalse(nothing["accepted_by_mechanical_evidence"])
        self.assertFalse(nothing["fully_verified"])
        partial = oracles.verdict(
            [
                {"layer": "invariants", "status": "pass"},
                {"layer": "backtest", "status": "not_applicable"},
            ]
        )
        self.assertTrue(partial["accepted_by_mechanical_evidence"])
        self.assertFalse(partial["fully_verified"])
        failed = oracles.verdict(
            [{"layer": "invariants", "status": "pass"}, {"layer": "replay", "status": "fail"}]
        )
        self.assertFalse(failed["accepted_by_mechanical_evidence"])
        self.assertIsNone(failed["realized_savings"])

    def test_fully_verified_requires_deep_receipt_and_no_unadjusted_layer(self):
        layers = [{"layer": "invariants", "status": "pass"}]
        self.assertTrue(oracles.verdict(layers, {"mode": "deep"})["fully_verified"])
        self.assertFalse(oracles.verdict(layers, {"mode": "cached"})["fully_verified"])
        self.assertFalse(oracles.verdict(layers)["fully_verified"])
        adjusted = [
            {"layer": "invariants", "status": "pass"},
            {"layer": "backtest", "status": "pass", "multiplicity": "holm-bonferroni"},
        ]
        self.assertTrue(oracles.verdict(adjusted, {"mode": "deep"})["fully_verified"])
        unadjusted = [{"layer": "backtest", "status": "pass", "multiplicity": "unadjusted"}]
        verdict = oracles.verdict(unadjusted, {"mode": "deep"})
        self.assertFalse(verdict["fully_verified"])
        self.assertEqual(verdict["unadjusted"], ["backtest"])


if __name__ == "__main__":
    unittest.main()
