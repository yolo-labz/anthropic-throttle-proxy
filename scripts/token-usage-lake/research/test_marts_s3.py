# ruff: noqa: S608, S324 - test SQL and md5 fixtures mirror the production checksums.

"""Offline tests for the marts builder and the object-store publisher.

The publisher talks to a real store through `mc`, so these tests put a fake `mc` on PATH that keeps
objects in a temporary directory. That covers the ordering, the immutability rule and every
verification branch without network access. `open_s3` needs a live endpoint and is proven by the
receipt in the vault instead.
"""

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import marts  # noqa: E402
import objectstore  # noqa: E402

FAKE_MC = """#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
from pathlib import Path

root = Path(os.environ["FAKE_S3_ROOT"])
argv = sys.argv[1:]
command = argv[0]
etag_override = os.environ.get("FAKE_MC_EMPTY_ETAG")


def local(uri):
    return root.joinpath(uri.replace("/", "__"))


if command == "cp":
    args = [a for a in argv[1:] if not a.startswith("--")]
    source = Path(args[0])
    if source.exists():  # upload: local source, remote target
        target = local(args[1])
    else:  # download: remote source, local target
        source, target = local(args[0]), Path(args[1])
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    sys.exit(0)

if command == "stat":
    path = local(argv[-1])
    if not path.is_file():
        print(json.dumps({"status": "error"}))
        sys.exit(1)
    payload = {"status": "success", "type": "file", "size": path.stat().st_size,
               "etag": hashlib.md5(path.read_bytes()).hexdigest()}
    if etag_override and etag_override in path.name:
        payload["etag"] = ""
    print(json.dumps(payload))
    sys.exit(0)

if command == "cat":
    sys.stdout.buffer.write(local(argv[-1]).read_bytes())
    sys.exit(0)

if command == "rm":
    local(argv[-1]).unlink(missing_ok=True)
    sys.exit(0)

print(f"fake mc: unsupported {argv}", file=sys.stderr)
sys.exit(2)
"""


def make_warehouse(path, *, rows=3):
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE calls(cwd VARCHAR, model VARCHAR, points DOUBLE, brief VARCHAR, note VARCHAR)"
    )
    connection.execute(
        "INSERT INTO calls VALUES "
        + ", ".join(
            f"('/home/secret/project', 'model-{index}', {index}.5, 'do the thing', 'keep')"
            for index in range(rows)
        )
    )
    connection.execute("CREATE TABLE meter_hourly(account VARCHAR, points DOUBLE)")
    connection.execute("INSERT INTO meter_hourly VALUES ('a', 1.0), ('a', 2.0)")
    connection.execute("CREATE SCHEMA wh")
    connection.execute("CREATE TABLE wh.dim_account(account VARCHAR)")
    connection.execute("INSERT INTO wh.dim_account VALUES ('a')")
    connection.close()


class MartsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.warehouse = self.tmp / "warehouse.duckdb"
        make_warehouse(self.warehouse)

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_drops_text_columns_and_records_digests(self):
        source = (("fact_calls", "main", "calls"), ("dim_account", "wh", "dim_account"))
        manifest = marts.build(self.warehouse, self.tmp / "out", sources=source)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(sorted(manifest["tables"]), ["dim_account", "fact_calls"])
        entry = manifest["tables"]["fact_calls"]
        self.assertEqual(entry["columns"], ["model", "points", "note"])
        self.assertEqual(manifest["dropped_columns"]["fact_calls"], ["cwd", "brief"])
        self.assertEqual(entry["rows"], 3)
        payload = (self.tmp / "out" / "fact_calls.parquet").read_bytes()
        self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(entry["md5"], hashlib.md5(payload).hexdigest())
        self.assertEqual(entry["bytes"], len(payload))
        expected = hashlib.sha256(self.warehouse.read_bytes()).hexdigest()
        self.assertEqual(manifest["source"]["sha256"], expected)

    def test_build_refuses_missing_source_and_dirty_output(self):
        with self.assertRaises(ValueError):
            marts.build(self.tmp / "absent.duckdb", self.tmp / "out")
        (self.tmp / "busy").mkdir()
        (self.tmp / "busy" / "leftover.txt").write_text("x")
        with self.assertRaises(ValueError):
            marts.build(self.warehouse, self.tmp / "busy")
        with self.assertRaises(ValueError):
            marts.build(self.warehouse, self.tmp / "out", sources=(("fact", "main", "nope"),))

    def test_denied_everything_is_refused(self):
        connection = duckdb.connect(str(self.tmp / "narrow.duckdb"))
        connection.execute("CREATE TABLE secret(cwd VARCHAR)")
        connection.execute("INSERT INTO secret VALUES ('/tmp')")
        connection.close()
        with self.assertRaises(ValueError):
            marts.build(
                self.tmp / "narrow.duckdb", self.tmp / "out", sources=(("x", "main", "secret"),)
            )


class ObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        self.store.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        mc = self.bin / "mc"
        mc.write_text(FAKE_MC)
        mc.chmod(mc.stat().st_mode | stat.S_IXUSR)
        self._env = dict(os.environ)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        os.environ["FAKE_S3_ROOT"] = str(self.store)
        os.environ.pop("FAKE_MC_EMPTY_ETAG", None)
        self.marts_dir = self.tmp / "marts"
        (self.marts_dir).mkdir()
        (self.marts_dir / "fact_calls.parquet").write_bytes(b"parquet-bytes")
        digest = hashlib.sha256(b"parquet-bytes").hexdigest()
        (self.marts_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "created_epoch": 1000.5,
                    "source": {"path": "w", "bytes": 1, "sha256": "a", "md5": "b"},
                    "dropped_columns": {},
                    "tables": {
                        "fact_calls": {
                            "source": "main.calls",
                            "bytes": len(b"parquet-bytes"),
                            "sha256": digest,
                            "md5": hashlib.md5(b"parquet-bytes").hexdigest(),
                            "rows": 1,
                            "columns": ["model"],
                        }
                    },
                }
            )
        )

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _publish(self):
        objectstore.publish(self.marts_dir, "fake/bucket/marts")
        return json.loads((self.marts_dir / "manifest.json").read_text())

    def test_split_base_rejects_s3_uri(self):
        with self.assertRaises(ValueError):
            objectstore.split_base("s3://token-lake/marts")
        self.assertEqual(
            objectstore.split_base("dokku-minio/token-lake/marts"),
            ("dokku-minio/token-lake/marts", "s3://token-lake/marts"),
        )

    def test_publish_then_reuse_is_idempotent(self):
        first = objectstore.publish(self.marts_dir, "fake/bucket/marts")
        self.assertFalse(first["reused"])
        self.assertTrue(first["verified"])
        self.assertEqual(first["objects"][0]["checksum"], "size+md5")
        self.assertEqual(first["run_id"], "1000-5")
        second = objectstore.publish(self.marts_dir, "fake/bucket/marts")
        self.assertTrue(second["reused"])

    def test_publish_refuses_a_missing_etag(self):
        os.environ["FAKE_MC_EMPTY_ETAG"] = "fact_calls.parquet"
        with self.assertRaises(ValueError) as caught:
            objectstore.publish(self.marts_dir, "fake/bucket/marts")
        self.assertIn("no ETag", str(caught.exception))

    def test_publish_refuses_changed_content_under_the_same_run_id(self):
        manifest = self._publish()
        manifest["tables"]["fact_calls"]["rows"] = 99
        (self.marts_dir / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ValueError) as caught:
            objectstore.publish(self.marts_dir, "fake/bucket/marts")
        self.assertIn("different content", str(caught.exception))

    def test_verify_refuses_a_wrong_size(self):
        manifest = self._publish()
        manifest["tables"]["fact_calls"]["bytes"] = 999
        with self.assertRaises(ValueError) as caught:
            objectstore.verify(manifest, "fake/bucket/marts", "1000-5")
        self.assertIn("size mismatch", str(caught.exception))

    def test_verify_refuses_a_tampered_object(self):
        manifest = self._publish()
        stored = self.store / "fake__bucket__marts__1000-5__fact_calls.parquet"
        stored.write_bytes(b"parquet-bytez")
        with self.assertRaises(ValueError) as caught:
            objectstore.verify(manifest, "fake/bucket/marts", "1000-5")
        self.assertIn("etag mismatch", str(caught.exception))

    def test_download_verifies_every_file(self):
        manifest = self._publish()
        target = self.tmp / "mirror"
        result = objectstore.download(target, "fake/bucket/marts", manifest=manifest)
        self.assertTrue(result["verified"])
        self.assertEqual((target / "fact_calls.parquet").read_bytes(), b"parquet-bytes")
        self.assertTrue((target / "manifest.json").is_file())

    def test_download_refuses_bytes_that_do_not_match(self):
        manifest = self._publish()
        stored = self.store / "fake__bucket__marts__1000-5__fact_calls.parquet"
        stored.write_bytes(b"parquet-bytez")
        with self.assertRaises(ValueError) as caught:
            objectstore.download(self.tmp / "mirror2", "fake/bucket/marts", manifest=manifest)
        self.assertIn("do not match the manifest", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
