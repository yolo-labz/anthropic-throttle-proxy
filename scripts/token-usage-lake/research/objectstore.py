"""Publish a marts directory to an S3-compatible store and open it read-only from anywhere.

Object store, not a shared file: readers never hold a lock on the writer's lake, and DuckDB's
httpfs reads only the byte ranges a query needs. Publishing is ordered so a consumer can never see
a half-published generation:

1. every mart object is uploaded under an immutable `<base>/<run_id>/` prefix,
2. each object's size and ETag is re-read from the store and compared with the manifest,
3. `manifest.json` (the commit marker) is uploaded last,
4. `<base>/latest.json` is replaced to point at it.

An existing `<run_id>` is immutable: publishing different bytes under the same id is refused.

Locations use the `mc` form `alias/bucket/prefix` because that is what the client resolves; the
`s3://bucket/prefix` URI DuckDB needs is derived from it. Passing an `s3://` path to `mc` is a
silent failure: mc treats the leading `s3:` as a *local directory name*, copies bytes to disk and
reports success with an empty ETag, so verification here refuses a missing ETag outright.

Credentials come from `S3_ENDPOINT`/`S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`, else from the `mc`
alias named by `S3_MC_ALIAS` (default `dokku-minio`). Nothing here ever logs a secret.
"""

# ruff: noqa: S608 - SET statements are built from validated settings; URIs are quote-escaped.

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import duckdb

DEFAULT_BASE = "dokku-minio/token-lake/marts"
MANIFEST = "manifest.json"
POINTER = "latest.json"


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _quote(value):
    return '"' + str(value).replace('"', '""') + '"'


def split_base(base):
    """Return (mc path, s3 uri) for a location. mc needs the alias form; DuckDB needs s3://."""
    parts = str(base).strip("/").split("/")
    if str(base).startswith("s3://") or len(parts) < 2 or not all(parts):
        raise ValueError(f"base must be <mc-alias>/<bucket>[/prefix], got {base!r}")
    return "/".join(parts), "s3://" + "/".join(parts[1:])


def _mc_binary():
    found = shutil.which("mc")
    if not found:
        raise RuntimeError(
            "the `mc` client is required to publish (nix shell nixpkgs#minio-client)"
        )
    return found


def _mc(*args, check=True):
    result = subprocess.run(  # noqa: S603 - fixed binary, list argv, no shell
        [_mc_binary(), *[str(arg) for arg in args]],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"mc {' '.join(str(a) for a in args)} failed: {result.stderr.strip()}")
    return result


def stat(uri):
    """Return {'size': int, 'etag': str} for one remote object, or None when it does not exist."""
    result = _mc("stat", "--json", uri, check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("status") == "success":
            if payload.get("type") != "file":
                return None
            return {"size": payload["size"], "etag": payload.get("etag", "")}
    return None


def credentials(alias=None):
    """Resolve S3 settings from the environment, else from an `mc` alias config."""
    endpoint = os.environ.get("S3_ENDPOINT")
    if endpoint and os.environ.get("S3_ACCESS_KEY_ID"):
        return {
            "endpoint": endpoint.split("://")[-1].rstrip("/"),
            "access_key": os.environ["S3_ACCESS_KEY_ID"],
            "secret_key": os.environ.get("S3_SECRET_ACCESS_KEY", ""),
            "use_ssl": "false" if endpoint.startswith("http://") else "true",
        }
    name = alias or os.environ.get("S3_MC_ALIAS", "dokku-minio")
    config = Path(os.environ.get("MC_CONFIG", Path.home() / ".mc" / "config.json"))
    try:
        row = json.loads(config.read_text())["aliases"][name]
    except (OSError, ValueError, KeyError) as error:
        raise RuntimeError(
            f"no S3 credentials: set S3_ENDPOINT/S3_ACCESS_KEY_ID or an mc alias named {name}"
        ) from error
    return {
        "endpoint": row["url"].split("://")[-1].rstrip("/"),
        "access_key": row["accessKey"],
        "secret_key": row["secretKey"],
        "use_ssl": "false" if row["url"].startswith("http://") else "true",
    }


def configure(connection, settings=None):
    """Point a DuckDB connection at the object store. Returns the settings actually used."""
    settings = settings or credentials()
    connection.execute("INSTALL httpfs")
    connection.execute("LOAD httpfs")
    connection.execute(f"SET s3_endpoint={_literal(settings['endpoint'])}")
    connection.execute(f"SET s3_use_ssl={_literal(settings['use_ssl'])}")
    connection.execute("SET s3_url_style='path'")
    connection.execute(f"SET s3_access_key_id={_literal(settings['access_key'])}")
    connection.execute(f"SET s3_secret_access_key={_literal(settings['secret_key'])}")
    return settings


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_sha256(uri):
    result = subprocess.run(  # noqa: S603 - fixed binary, list argv
        [_mc_binary(), "cat", uri], capture_output=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"mc cat {uri} failed")
    return hashlib.sha256(result.stdout).hexdigest()


def _object_mode(entry, observed, *, uri, deep):
    if observed["size"] != entry["bytes"]:
        raise ValueError(f"size mismatch for {uri}: {observed['size']} != {entry['bytes']}")
    etag = observed["etag"]
    if not etag:
        raise ValueError(
            f"object store returned no ETag for {uri}; refusing to certify a local copy"
        )
    single_part = len(etag) == 32 and "-" not in etag
    if single_part and etag != entry["md5"]:
        raise ValueError(f"etag mismatch for {uri}: {etag} != {entry['md5']}")
    mode = "size+md5" if single_part else "size+multipart-etag"
    if deep:
        if _remote_sha256(uri) != entry["sha256"]:
            raise ValueError(f"sha256 mismatch for {uri}")
        mode = f"{mode}+sha256"
    return mode


def verify(manifest, base, run_id, *, deep=False):
    """Compare every published object with the manifest. Returns a receipt."""
    mc_base, _ = split_base(base)
    checked = []
    for name, entry in sorted(manifest["tables"].items()):
        uri = f"{mc_base}/{run_id}/{name}.parquet"
        observed = stat(uri)
        if observed is None:
            raise ValueError(f"missing object: {uri}")
        mode = _object_mode(entry, observed, uri=uri, deep=deep)
        checked.append({"table": name, "bytes": entry["bytes"], "checksum": mode})
    local = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return {
        "run_id": run_id,
        "base": base,
        "s3_uri": split_base(base)[1] + f"/{run_id}",
        "manifest_sha256": hashlib.sha256(local.encode()).hexdigest(),
        "objects": checked,
        "verified": True,
    }


def publish(marts_dir, base=DEFAULT_BASE, run_id=None, *, deep=False, dry_run=False):
    """Upload `<marts_dir>` as an immutable generation. Returns a verification receipt."""
    mc_base, _ = split_base(base)
    marts_dir = Path(marts_dir)
    manifest_path = marts_dir / MANIFEST
    if not manifest_path.is_file():
        raise ValueError(f"no manifest in {marts_dir}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != 1:
        raise ValueError("unsupported marts manifest version")
    run_id = run_id or str(manifest["created_epoch"]).replace(".", "-")
    destination = f"{mc_base}/{run_id}"

    existing = stat(f"{destination}/{MANIFEST}")
    if existing is not None:
        remote = json.loads(_mc("cat", f"{destination}/{MANIFEST}").stdout)
        if json.dumps(remote, sort_keys=True) != json.dumps(manifest, sort_keys=True):
            raise ValueError(f"generation {run_id} already exists with different content")
        return dict(verify(manifest, base, run_id, deep=deep), reused=True)
    if dry_run:
        return {
            "run_id": run_id,
            "base": base,
            "s3_uri": split_base(base)[1] + f"/{run_id}",
            "objects": len(manifest["tables"]),
            "verified": False,
        }

    for name in sorted(manifest["tables"]):
        _mc("cp", "--quiet", marts_dir / f"{name}.parquet", f"{destination}/{name}.parquet")
    _mc("cp", "--quiet", manifest_path, f"{destination}/{MANIFEST}")
    receipt = verify(manifest, base, run_id, deep=deep)
    pointer = {"run_id": run_id, "manifest_sha256": receipt["manifest_sha256"]}
    pointer_path = marts_dir / POINTER
    pointer_path.write_text(json.dumps(pointer, sort_keys=True, indent=2) + "\n")
    if stat(f"{mc_base}/{POINTER}") is not None:
        _mc("rm", f"{mc_base}/{POINTER}")
    _mc("cp", "--quiet", pointer_path, f"{mc_base}/{POINTER}")
    return dict(receipt, reused=False)


def download(destination, base=DEFAULT_BASE, run_id=None, manifest=None):
    """Mirror one published generation to local Parquet for tools that cannot speak S3.

    Every file's SHA-256 is re-checked against the manifest after the copy, so a truncated or
    swapped download fails here instead of quietly feeding a dashboard.
    """
    mc_base, _ = split_base(base)
    if manifest is None:
        run_id, manifest = read_manifest(base, run_id)
    run_id = run_id or str(manifest["created_epoch"]).replace(".", "-")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, entry in sorted(manifest["tables"].items()):
        target = destination / f"{name}.parquet"
        _mc("cp", "--quiet", f"{mc_base}/{run_id}/{name}.parquet", target)
        if _sha256_file(target) != entry["sha256"]:
            raise ValueError(f"downloaded bytes for {name} do not match the manifest")
    (destination / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {
        "run_id": run_id,
        "path": str(destination),
        "tables": sorted(manifest["tables"]),
        "verified": True,
    }


def read_manifest(base=DEFAULT_BASE, run_id=None, settings=None):
    """Read a published manifest straight from the store with DuckDB, no `mc` needed."""
    _, s3_base = split_base(base)
    connection = duckdb.connect(":memory:")
    configure(connection, settings)
    if run_id is None:
        pointer = json.loads(
            connection.execute(
                f"SELECT content FROM read_text({_literal(f'{s3_base}/{POINTER}')})"
            ).fetchone()[0]
        )
        run_id = pointer["run_id"]
    content = connection.execute(
        f"SELECT content FROM read_text({_literal(f'{s3_base}/{run_id}/{MANIFEST}')})"
    ).fetchone()[0]
    connection.close()
    return run_id, json.loads(content)


def materialize(path, base=DEFAULT_BASE, run_id=None, *, tables=None, settings=None):
    """Copy one verified generation into a local DuckDB file that point-and-click tools can open.

    BI tools that cannot configure httpfs credentials still get the same bytes: the file records
    the generation it came from in `_generation`, so a stale dashboard is provable, not guessed.
    """
    path = Path(path)
    if path.exists():
        raise ValueError(f"refusing to overwrite {path}")
    connection, manifest = open_s3(base, run_id, tables=tables, settings=settings)
    connection.execute(f"ATTACH {_literal(path)} AS marts")
    for table in sorted(manifest["tables"]):
        if tables and table not in tables:
            continue
        connection.execute(f"CREATE TABLE marts.{_quote(table)} AS SELECT * FROM {_quote(table)}")
    connection.execute(
        "CREATE TABLE marts._generation(run_id VARCHAR, s3_uri VARCHAR, manifest_sha256 VARCHAR)"
    )
    connection.execute(
        "INSERT INTO marts._generation VALUES (?, ?, ?)",
        [manifest["run_id"], manifest["s3_uri"], manifest["manifest_sha256"]],
    )
    connection.execute("DETACH marts")
    connection.close()
    return {"path": str(path), "run_id": manifest["run_id"], "tables": sorted(manifest["tables"])}


def open_s3(
    base=DEFAULT_BASE, run_id=None, *, tables=None, threads=2, memory_limit="1GB", settings=None
):
    """Open every declared mart as a read-only view over the object store."""
    _, s3_base = split_base(base)
    run_id, manifest = read_manifest(base, run_id, settings)
    connection = duckdb.connect(
        ":memory:", config={"threads": threads, "memory_limit": memory_limit}
    )
    configure(connection, settings)
    declared = sorted(manifest["tables"])
    selected = tables or declared
    unknown = sorted(set(selected) - set(declared))
    if unknown:
        raise ValueError(f"not in published generation: {', '.join(unknown)}")
    for table in selected:
        connection.execute(
            f"CREATE VIEW {_quote(table)} AS SELECT * FROM read_parquet("
            f"{_literal(f'{s3_base}/{run_id}/{table}.parquet')}, union_by_name=true)"
        )
    return connection, dict(
        manifest,
        run_id=run_id,
        base=base,
        s3_uri=f"{s3_base}/{run_id}",
        manifest_sha256=hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE, help="mc alias/bucket/prefix")
    sub = parser.add_subparsers(dest="command", required=True)
    publish_cmd = sub.add_parser(
        "publish", help="upload a marts directory as an immutable generation"
    )
    publish_cmd.add_argument("marts_dir")
    publish_cmd.add_argument("--run-id")
    publish_cmd.add_argument(
        "--deep", action="store_true", help="re-hash every object after upload"
    )
    download_cmd = sub.add_parser("download", help="mirror a published generation to local Parquet")
    download_cmd.add_argument("destination")
    download_cmd.add_argument("--run-id")
    materialize_cmd = sub.add_parser(
        "materialize", help="copy a generation into one local DuckDB file"
    )
    materialize_cmd.add_argument("path")
    materialize_cmd.add_argument("--run-id")
    verify_cmd = sub.add_parser("verify", help="compare a published generation with its manifest")
    verify_cmd.add_argument("--run-id")
    verify_cmd.add_argument("--deep", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "publish":
        result = publish(args.marts_dir, args.base, args.run_id, deep=args.deep)
    elif args.command == "download":
        result = download(args.destination, args.base, args.run_id)
    elif args.command == "materialize":
        result = materialize(args.path, args.base, args.run_id)
    else:
        run_id, manifest = read_manifest(args.base, args.run_id)
        result = verify(manifest, args.base, run_id, deep=args.deep)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
