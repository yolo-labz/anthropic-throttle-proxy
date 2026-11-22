"""Build the reader-facing marts layer as Parquet from a DuckDB warehouse.

The source is opened READ ONLY and is never written. Text-bearing columns are dropped by name, so
paths, briefs and prompt preludes cannot leak into a mart that a dashboard or an object store
might carry. Every published file is hashed (SHA-256 and MD5) and counted, and the manifest records
the source file identity, so a reader can prove it is looking at the same bytes the builder made.

This is a desktop-side derivation, not a second lake writer: it reads a warehouse copy and writes a
new directory. It never opens the live lake, never schedules anything, and never activates a
collector.
"""

# ruff: noqa: S608 - identifiers are validated by `_quote`; paths are single-quote escaped.

import hashlib
import json
import time
from pathlib import Path

import duckdb
from snapshots import atomic_json

MARTS_VERSION = 1

# Any column whose name matches is dropped before export, case-insensitively.
DENY_COLUMNS = frozenset(
    {"brief", "prelude", "cwd", "path", "prompt", "text", "content", "body", "raw", "message"}
)

# (mart name, source schema, source table)
SOURCES = (
    ("fact_calls", "main", "calls"),
    ("fact_requests", "main", "requests"),
    ("fact_tasks", "main", "tasks"),
    ("fact_meter_hourly", "main", "meter_hourly"),
    ("fact_points_hourly", "main", "points_hourly"),
    ("fact_tool_use", "main", "tool_use"),
    ("fact_limit_state", "main", "limit_state"),
    ("dim_account", "wh", "dim_account"),
    ("dim_effort", "wh", "dim_effort"),
)

# The modeled warehouse carries a `wh` schema of dimensions; the live lake does not. Derive the
# same key-only contract from the facts rather than failing on a table the mart never needed.
DERIVED = {
    "dim_account": (
        "SELECT DISTINCT account AS account_key FROM main.requests "
        "WHERE account IS NOT NULL ORDER BY 1"
    ),
    "dim_effort": (
        "SELECT DISTINCT effort AS effort_key FROM main.tasks WHERE effort IS NOT NULL ORDER BY 1"
    ),
}


def _quote(value):
    return '"' + str(value).replace('"', '""') + '"'


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _digests(path):
    sha, md5 = hashlib.sha256(), hashlib.new("md5", usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def _columns(connection, schema, table):
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, table],
    ).fetchall()
    if not rows:
        raise ValueError(f"missing source table: {schema}.{table}")
    return [row[0] for row in rows]


def _select_sql(connection, mart, schema, table):
    """Return (sql, columns, source label, dropped columns) for a mart.

    A dimension that the warehouse does not carry is derived from the facts, so the live lake and
    a modeled warehouse produce the same mart contract.
    """
    try:
        columns = _columns(connection, schema, table)
    except ValueError:
        if mart not in DERIVED:
            raise
        return DERIVED[mart], None, "derived", []
    kept = [name for name in columns if name.lower() not in DENY_COLUMNS]
    removed = [name for name in columns if name not in kept]
    if not kept:
        raise ValueError(f"every column of {schema}.{table} is denied")
    select = ", ".join(_quote(name) for name in kept)
    return (
        f"SELECT {select} FROM {_quote(schema)}.{_quote(table)}",
        kept,
        f"{schema}.{table}",
        removed,
    )


def build(warehouse, out_dir, *, sources=None, created_epoch=None, provenance=None):
    """Write `<out>/<mart>.parquet` for each source plus a manifest. Returns the manifest."""
    warehouse = Path(warehouse)
    out = Path(out_dir)
    if not warehouse.is_file():
        raise ValueError(f"warehouse not found: {warehouse}")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)

    source_sha256, source_md5 = _digests(warehouse)
    connection = duckdb.connect(str(warehouse), read_only=True)
    tables, dropped = {}, {}
    try:
        for mart, schema, table in sources or SOURCES:
            select, kept, source, removed = _select_sql(connection, mart, schema, table)
            target = out / f"{mart}.parquet"
            connection.execute(
                f"COPY ({select}) TO {_literal(target)} (FORMAT parquet, COMPRESSION zstd)"
            )
            rows = connection.execute(f"SELECT count(*) FROM ({select}) AS q").fetchone()[0]
            exported = connection.execute(f"SELECT * FROM ({select}) AS q LIMIT 0").description
            exported_columns = kept if kept is not None else [desc[0] for desc in exported]
            sha256, md5 = _digests(target)
            tables[mart] = {
                "source": source,
                "bytes": target.stat().st_size,
                "sha256": sha256,
                "md5": md5,
                "rows": rows,
                "columns": exported_columns,
            }
            if removed:
                dropped[mart] = removed
    finally:
        connection.close()

    manifest = {
        "version": MARTS_VERSION,
        "created_epoch": time.time() if created_epoch is None else created_epoch,
        "source": {
            "path": str(warehouse),
            "bytes": warehouse.stat().st_size,
            "sha256": source_sha256,
            "md5": source_md5,
        },
        "dropped_columns": dropped,
        "tables": tables,
    }
    if provenance is not None:
        manifest["provenance"] = provenance
    atomic_json(out / "manifest.json", manifest)
    return manifest


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--provenance",
        help="JSON file whose contents are recorded in the manifest (e.g. the lake run receipt)",
    )
    args = parser.parse_args(argv)
    provenance = None
    if args.provenance:
        provenance = json.loads(Path(args.provenance).read_text())
    manifest = build(args.warehouse, args.out, provenance=provenance)
    print(json.dumps({"tables": len(manifest["tables"]), "out": args.out}, indent=2))


if __name__ == "__main__":
    main()
