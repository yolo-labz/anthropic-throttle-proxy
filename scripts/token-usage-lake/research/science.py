"""Notebook helpers. Dataset snapshots and desktop compute are explicit choices."""

import hashlib
import json
import os
from pathlib import Path


def snapshot_generation(root=None, run_id=None):
    """Resolve one generation and validate its structure. Does not hash file bodies."""
    root = Path(root or os.environ.get("SCIENCE_SNAPSHOTS", "/workspace/data/snapshots"))
    run_id = run_id if run_id is not None else os.environ.get("SCIENCE_SNAPSHOT_RUN_ID")
    pointer = None
    if run_id is None:
        pointer = json.loads((root / "latest.json").read_text())
        run_id = pointer.get("run_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or any(c not in "0123456789abcdef-" for c in run_id)
    ):
        raise ValueError("invalid snapshot identity")
    snapshot = root / run_id
    if snapshot.is_symlink():
        raise ValueError("symlink snapshot")
    if not snapshot.is_dir():
        raise ValueError("snapshot generation missing")
    manifest_path = snapshot / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("symlink snapshot manifest")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != 1 or manifest.get("run_id") != run_id:
        raise ValueError("snapshot manifest identity/version mismatch")
    if pointer is not None and pointer != manifest:
        raise ValueError("latest pointer differs from immutable manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("snapshot manifest has no files")
    for name in files:
        declared_path(snapshot, name)
    return snapshot, manifest


def declared_path(snapshot, name):
    """Validate one declared relative path and return it; refuses escapes and symlinks."""
    relative = Path(name)
    if (
        relative.as_posix() != name
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix != ".parquet"
    ):
        raise ValueError("invalid snapshot file path")
    if any(
        (Path(snapshot) / Path(*relative.parts[:i])).is_symlink()
        for i in range(1, len(relative.parts) + 1)
    ):
        raise ValueError("symlink snapshot file")
    return Path(snapshot) / relative


def verify_files(snapshot, manifest):
    """Hash every declared file and compare it with the immutable manifest."""
    for name, expected in manifest["files"].items():
        path = declared_path(snapshot, name)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != expected["bytes"] or digest != expected["sha256"]:
            raise ValueError("snapshot file checksum mismatch")


def latest_snapshot(root=None, run_id=None):
    snapshot, manifest = snapshot_generation(root, run_id)
    verify_files(snapshot, manifest)
    return snapshot, manifest


def parquet_paths(snapshot, manifest, table):
    """Only declared files, never a glob that can silently include extra data."""
    paths = [
        str(Path(snapshot) / name)
        for name in manifest["files"]
        if name == table + ".parquet" or name.startswith(table + "/")
    ]
    if not paths:
        raise ValueError(f"snapshot has no declared files for {table}")
    return sorted(paths)


def tables(manifest):
    """Table names implied by declared files, e.g. calls/day_utc=.../part-0.parquet."""
    names = set()
    for name in manifest["files"]:
        head = name.split("/", 1)[0]
        names.add(head[: -len(".parquet")] if head.endswith(".parquet") else head)
    return sorted(names)


def desktop_client():
    from distributed import Client, Security

    root = Path(os.environ.get("SCIENCE_TLS", "/run/science-tls"))
    security = Security(
        tls_ca_file=str(root / "ca.crt"),
        tls_client_cert=str(root / "client.crt"),
        tls_client_key=str(root / "client.key"),
        require_encryption=True,
    )
    return Client(os.environ["SCIENCE_SCHEDULER"], security=security, timeout="15s")


def configure_tracking():
    import mlflow

    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    return mlflow
