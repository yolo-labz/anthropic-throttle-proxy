"""Offline tests for the marts builder and the object-store publisher.

The publisher speaks S3 over HTTP, so these tests run a fake store on a loopback port and keep
objects in memory. That covers the ordering, the immutability rule and every verification branch
without network access — and unlike a fake `mc` binary, it proves the bytes actually left the
process instead of landing in a local directory named `s3:`.
"""

import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb
import marts
import objectstore


def _md5(payload):
    return hashlib.new("md5", payload, usedforsecurity=False).hexdigest()


class FakeS3:
    """A tiny S3 endpoint: PUT, GET, HEAD and DELETE over a dict, no signing checks."""

    def __init__(self):
        self.objects = {}
        self.suppress_etag = set()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self):
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handler(self):
        store = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _key(self):
                parts = self.path.lstrip("/").split("/", 1)
                return (parts[0], parts[1] if len(parts) > 1 else "")

            def _etag(self, bucket, key, payload):
                if f"{bucket}/{key}" in store.suppress_etag:
                    return {}
                return {"ETag": '"' + _md5(payload) + '"'}

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = self.rfile.read(length) if length else b""
                bucket, key = self._key()
                store.objects[(bucket, key)] = payload
                self._reply(200, self._etag(bucket, key, payload))

            def do_GET(self):
                bucket, key = self._key()
                payload = store.objects.get((bucket, key))
                if payload is None:
                    self._reply(404, {})
                    return
                headers = {"Content-Length": str(len(payload)), **self._etag(bucket, key, payload)}
                self._reply(200, headers, payload)

            def do_HEAD(self):
                bucket, key = self._key()
                payload = store.objects.get((bucket, key))
                if payload is None:
                    self._reply(404, {})
                    return
                headers = {"Content-Length": str(len(payload)), **self._etag(bucket, key, payload)}
                self._reply(200, headers)

            def do_DELETE(self):
                store.objects.pop(self._key(), None)
                self._reply(204, {})

            def _reply(self, status, headers, payload=b""):
                self.send_response(status)
                if "Content-Length" not in headers:
                    headers = {**headers, "Content-Length": str(len(payload))}
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

        return Handler


def make_warehouse(path, *, rows=3):
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE calls(cwd VARCHAR, model VARCHAR, points DOUBLE, brief VARCHAR, note VARCHAR)"
    )
    connection.executemany(
        "INSERT INTO calls VALUES (?, ?, ?, ?, ?)",
        [
            ("/home/secret/project", f"model-{index}", index + 0.5, "do the thing", "keep")
            for index in range(rows)
        ],
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
        self.assertEqual(entry["md5"], _md5(payload))
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

    def test_dims_are_derived_when_the_wh_schema_is_absent(self):
        connection = duckdb.connect(str(self.tmp / "lake.duckdb"))
        connection.execute("CREATE TABLE requests(account VARCHAR, model VARCHAR)")
        connection.execute("INSERT INTO requests VALUES ('a', 'm'), ('b', 'm'), ('a', 'm')")
        connection.execute("CREATE TABLE tasks(effort VARCHAR)")
        connection.execute("INSERT INTO tasks VALUES ('high'), ('off')")
        connection.close()
        source = (("dim_account", "wh", "dim_account"), ("dim_effort", "wh", "dim_effort"))
        manifest = marts.build(
            self.tmp / "lake.duckdb",
            self.tmp / "out",
            sources=source,
            provenance={"run_id": "r1", "quality_status": "failed"},
        )
        self.assertEqual(manifest["tables"]["dim_account"]["source"], "derived")
        self.assertEqual(manifest["tables"]["dim_account"]["rows"], 2)
        self.assertEqual(manifest["tables"]["dim_effort"]["rows"], 2)
        self.assertEqual(manifest["tables"]["dim_effort"]["columns"], ["effort_key"])
        self.assertEqual(manifest["provenance"]["quality_status"], "failed")

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
        self.fake = FakeS3().__enter__()
        self._env = dict(os.environ)
        os.environ["S3_ENDPOINT"] = f"http://{self.fake.endpoint}"
        os.environ["S3_ACCESS_KEY_ID"] = "test-key"
        os.environ["S3_SECRET_ACCESS_KEY"] = uuid.uuid4().hex
        self.marts_dir = self.tmp / "marts"
        self.marts_dir.mkdir()
        payload = b"parquet-bytes"
        (self.marts_dir / "fact_calls.parquet").write_bytes(payload)
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
                            "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "md5": _md5(payload),
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
        self.fake.__exit__(None, None, None)
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
        self.assertIn(("bucket", "marts/1000-5/fact_calls.parquet"), self.fake.objects)
        self.assertIn(("bucket", "marts/1000-5/manifest.json"), self.fake.objects)
        self.assertIn(("bucket", "marts/latest.json"), self.fake.objects)
        second = objectstore.publish(self.marts_dir, "fake/bucket/marts")
        self.assertTrue(second["reused"])

    def test_publish_refuses_a_missing_etag(self):
        self.fake.suppress_etag.add("bucket/marts/1000-5/fact_calls.parquet")
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
        self.fake.objects[("bucket", "marts/1000-5/fact_calls.parquet")] = b"parquet-bytez"
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
        self.fake.objects[("bucket", "marts/1000-5/fact_calls.parquet")] = b"parquet-bytez"
        with self.assertRaises(ValueError) as caught:
            objectstore.download(self.tmp / "mirror2", "fake/bucket/marts", manifest=manifest)
        self.assertIn("do not match the manifest", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
