"""Publish immutable research datasets without opening the production DuckDB file."""

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import duckdb

TABLES = (
    "calls",
    "requests",
    "tasks",
    "tool_invocations",
    "meter_hourly",
    "limit_state",
    "lake_watermark",
    "burn_hourly",
    "tool_use",
    "timing_hourly",
)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def file_identity(path):
    value = path.stat()
    return value.st_ino, value.st_size, value.st_mtime_ns


def series_relation(con, table, seconds, boundary):
    """Separate source grains; missing measures stay NULL and bins carry coverage."""
    if seconds not in (60, 3600, 86400):
        raise ValueError("unsupported bucket size")
    if table == "calls":
        dimensions = "provider, model, effort_used"
        measures = (
            'count(*) as events, count("input") as input_observed, '
            'sum("input") as input_tokens, sum("output") as output_tokens, '
            "sum(cache_read) as cache_read_tokens, sum(cache_write) as cache_write_tokens"
        )
    elif table == "requests":
        dimensions = "account, model, status"
        measures = (
            'count(*) as events, sum("input") as input_tokens, '
            'sum("output") as output_tokens, sum(list_usd) as estimated_list_usd'
        )
    elif table == "tool_invocations":
        dimensions = "source, tool, status"
        measures = (
            "count(*) as events, count(error) as error_observed, "
            "count_if(error) as errors, median(duration_ms) as median_duration_ms"
        )
    else:
        raise ValueError("unsupported source grain")
    # Source timestamps are plain BRT, explicitly converted to epoch UTC.
    bins = con.table(table).project(
        f"*, floor((epoch(at_brt) + 10800) / {seconds}) * {seconds} AS bucket_epoch"
    )
    return bins.aggregate(
        f"bucket_epoch, {seconds} AS bucket_seconds, {dimensions}, {measures}, "
        f"bucket_epoch + {seconds} <= {float(boundary)} AS bucket_closed_by_watermark",
        group_expr="bucket_epoch, " + dimensions,
    )


def publish(lake_root, output_root):
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    with (output_root / ".publish.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _publish(lake_root, output_root)


def _publish(lake_root, output_root):
    lake_root, output_root = Path(lake_root), Path(output_root)
    receipt_path = lake_root / "runtime/latest-complete.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    run_id = receipt["run_id"]
    if (
        not isinstance(run_id, str)
        or not run_id
        or any(c not in "0123456789abcdef-" for c in run_id)
    ):
        raise ValueError("invalid completed run identity")
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / run_id
    if destination.exists():
        manifest = json.loads((destination / "manifest.json").read_text())
        atomic_json(output_root / "latest.json", manifest)
        return manifest
    source = lake_root / "lake.duckdb"
    wal = source.with_suffix(".duckdb.wal")
    before = file_identity(source)
    if wal.exists() or before[2] / 1e9 > receipt["completed_epoch"]:
        raise RuntimeError("writer active; snapshot deferred")
    if shutil.disk_usage(output_root).free < max(512 * 1024**2, before[1] * 4):
        raise OSError("insufficient snapshot disk headroom")
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=output_root) as temporary:
        stage = Path(temporary)
        copied = stage / "source.duckdb"
        shutil.copyfile(source, copied)
        if (
            wal.exists()
            or file_identity(source) != before
            or receipt_path.read_bytes() != receipt_bytes
        ):
            raise RuntimeError("source changed during copy; snapshot deferred")
        counts = {}
        with duckdb.connect(
            str(copied), read_only=True, config={"threads": 1, "memory_limit": "512MB"}
        ) as con:
            for table, expected in receipt["fact_counts"].items():
                if table not in TABLES:
                    raise ValueError("unexpected receipt table")
                if con.table(table).count("*").fetchall()[0][0] != expected:
                    raise RuntimeError("copy does not match accepted run")
            boundary = receipt["observed_through_epoch"]
            actual = con.execute(
                "SELECT min(epoch(observed_through_brt) + 10800) FROM lake_watermark"
            ).fetchone()[0]
            if actual != boundary:
                raise RuntimeError("copy watermark does not match accepted run")
            available = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            for table in TABLES:
                if table not in available:
                    continue
                relation = con.table(table)
                counts[table] = relation.count("*").fetchall()[0][0]
                columns = relation.columns
                if "at_brt" in columns:
                    relation.project("*, cast(at_brt + interval 3 hour AS DATE) AS day_utc").order(
                        "ALL"
                    ).write_parquet(
                        str(stage / table), compression="zstd", partition_by=["day_utc"]
                    )
                else:
                    relation.order("ALL").write_parquet(
                        str(stage / (table + ".parquet")), compression="zstd"
                    )
            for table in ("calls", "requests", "tool_invocations"):
                for seconds in (60, 3600, 86400):
                    relation = series_relation(con, table, seconds, boundary)
                    name = f"{table}_{seconds}s"
                    relation.project(
                        "*, cast(to_timestamp(bucket_epoch) AT TIME ZONE 'UTC' AS DATE) AS day_utc"
                    ).order("ALL").write_parquet(
                        str(stage / name), compression="zstd", partition_by=["day_utc"]
                    )
        with copied.open("rb") as stream:
            source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        copied.unlink()
        files = {}
        objects = output_root / ".objects"
        objects.mkdir(exist_ok=True)
        for path in sorted(stage.rglob("*.parquet")):
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files[path.relative_to(stage).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
            stored = objects / (digest + ".parquet")
            if stored.exists():
                path.unlink()
                os.link(stored, path)
            else:
                os.link(path, stored)
        manifest = {
            "version": 1,
            "run_id": run_id,
            "published_epoch": time.time(),
            "observed_through_epoch": boundary,
            "completed_epoch": receipt["completed_epoch"],
            "source_fingerprint": receipt["input_fingerprint"],
            "source_sha256": source_hash,
            "counts": counts,
            "data_quality": receipt.get("data_quality", {"source_freshness_1200": "not_recorded"}),
            "files": files,
            "timezone": "UTC",
            "missing_bins": "unobserved, not zero",
            "source_grains": "native calls, gateway requests and tool invocations are separate",
        }
        atomic_json(stage / "manifest.json", manifest)
        os.rename(stage, destination)
    atomic_json(output_root / "latest.json", manifest)
    return manifest


if __name__ == "__main__":
    root = os.environ.get("LAKE_ROOT", "/lake")
    out = os.environ.get("SCIENCE_SNAPSHOTS", "/workspace/data/snapshots")
    failures = 0
    while True:
        try:
            result = publish(root, out)
            print(
                json.dumps({"run_id": result["run_id"], "datasets": len(result["files"])}),
                flush=True,
            )
            failures = 0
        except (OSError, ValueError, RuntimeError, duckdb.Error) as error:
            failures += 1
            print(
                json.dumps({"snapshot_deferred": type(error).__name__, "reason": str(error)[:220]}),
                flush=True,
            )
            if failures >= 3:
                raise
        time.sleep(30)
