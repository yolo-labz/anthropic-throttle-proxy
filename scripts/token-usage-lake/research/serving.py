"""Read-only serving over a verified generation. The live lake writer is never opened.

Contract (see README): publish an immutable generation, verify its declared file hashes once,
open only declared Parquet as views, and revalidate cheaply on every open. A reader that finds a
manifest/file mismatch refuses instead of serving. Verification receipts live outside the
generation so a reader can never mutate what the publisher owns.
"""

# ruff: noqa: S608 - view names come from a verified manifest; file paths are single-quote escaped.

import hashlib
import json
import os
import time
from pathlib import Path

import duckdb
import science
from snapshots import atomic_json

RECEIPT_VERSION = 2
CHAIN_NAME = "chain.jsonl"


def _receipt_path(snapshot, state_root=None):
    root = Path(state_root or os.environ.get("SCIENCE_VERIFY_STATE") or snapshot.parent / ".verify")
    return root / (snapshot.name + ".json")


def manifest_sha256(snapshot):
    return hashlib.sha256((Path(snapshot) / "manifest.json").read_bytes()).hexdigest()


def _digest(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_identity(snapshot, manifest):
    """Cheap revalidation data: identity of each declared file, no hashing.

    `ctime` is included because it cannot be restored by userspace: rewriting a file and putting
    the old mtime back still moves ctime, so the in-place tamper route invalidates the receipt.
    """
    identity = {}
    for name in manifest["files"]:
        value = science.declared_path(snapshot, name).stat()
        identity[name] = [value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]
    return identity


def _chain_file(snapshot, state_root=None):
    return _receipt_path(snapshot, state_root).parent / CHAIN_NAME


def _chain_entries(path):
    """Hash-chained append-only receipt log: each entry commits to the one before it."""
    if not path.exists():
        return []
    entries, previous = [], None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as error:
            raise ValueError("verification chain is corrupt") from error
        body = {
            key: entry.get(key)
            for key in ("seq", "run_id", "manifest_sha256", "receipt_sha256", "prev")
        }
        if entry.get("entry_sha256") != _digest(body):
            raise ValueError("verification chain is corrupt")
        if previous is not None and entry.get("prev") != previous:
            raise ValueError("verification chain is corrupt")
        previous = entry["entry_sha256"]
        entries.append(entry)
    return entries


def _append_chain(snapshot, receipt_sha256, manifest_sha, state_root=None):
    path = _chain_file(snapshot, state_root)
    entries = _chain_entries(path)
    body = {
        "seq": len(entries),
        "run_id": snapshot.name,
        "manifest_sha256": manifest_sha,
        "receipt_sha256": receipt_sha256,
        "prev": entries[-1]["entry_sha256"] if entries else "",
    }
    entry = dict(body, entry_sha256=_digest(body))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


# ponytail: cheap revalidation trusts inode/size/mtime/ctime plus an entry in the receipt chain, so
# a forger with write access to BOTH the generation and the state root still wins. Ceiling: that
# attacker class; the chain does force them to rewrite every later entry, and it detects accidental
# damage or a single forged receipt. Upgrade: `deep=True`, or move the state root to a different
# trust domain and sign the receipt with a key the serving process does not hold.
def _cached(snapshot, manifest, state_root=None):
    path = _receipt_path(snapshot, state_root)
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if receipt.get("version") != RECEIPT_VERSION or receipt.get("run_id") != manifest["run_id"]:
        return None
    manifest_sha = manifest_sha256(snapshot)
    if receipt.get("manifest_sha256") != manifest_sha:
        return None
    if receipt.get("file_identity") != _file_identity(snapshot, manifest):
        return None
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    if not any(
        entry["receipt_sha256"] == observed
        and entry["manifest_sha256"] == manifest_sha
        and entry["run_id"] == snapshot.name
        for entry in _chain_entries(_chain_file(snapshot, state_root))
    ):
        return None
    return receipt


def verify(root=None, run_id=None, *, deep=False, state_root=None, epoch=None):
    """Return a verification receipt. `deep` re-hashes; otherwise a matching receipt is reused.

    Reuse is keyed on the immutable manifest hash, the inode/size/mtime/ctime of every declared
    file, and the receipt's presence in the hash-chained receipt log, so a changed, replaced or
    rewritten file always triggers a fresh deep verification.
    """
    snapshot, manifest = science.snapshot_generation(root, run_id)
    if not deep:
        receipt = _cached(snapshot, manifest, state_root)
        if receipt is not None:
            return dict(receipt, mode="cached", tables=science.tables(manifest))
    science.verify_files(snapshot, manifest)
    manifest_sha = manifest_sha256(snapshot)
    receipt = {
        "version": RECEIPT_VERSION,
        "run_id": manifest["run_id"],
        "manifest_sha256": manifest_sha,
        "verified_epoch": time.time() if epoch is None else epoch,
        "mode": "deep",
        "file_identity": _file_identity(snapshot, manifest),
        "files": {name: dict(value) for name, value in manifest["files"].items()},
        "tables": science.tables(manifest),
    }
    path = _receipt_path(snapshot, state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, receipt)
    _append_chain(snapshot, hashlib.sha256(path.read_bytes()).hexdigest(), manifest_sha, state_root)
    return receipt


def open_verified(
    root=None,
    run_id=None,
    *,
    deep=False,
    tables=None,
    threads=2,
    memory_limit="1GB",
    state_root=None,
    epoch=None,
):
    """Open declared Parquet as views in a private in-memory database.

    Returns (connection, receipt). Reads never touch the live lake file, and every query is
    bounded by the caller's thread/memory limits so a dashboard cannot starve the writer host.
    """
    receipt = verify(root, run_id, deep=deep, state_root=state_root, epoch=epoch)
    snapshot, _manifest = science.snapshot_generation(root, receipt["run_id"])
    manifest = {"files": receipt["files"]}
    selected = tables or receipt["tables"]
    unknown = sorted(set(selected) - set(receipt["tables"]))
    if unknown:
        raise ValueError(f"not in verified generation: {', '.join(unknown)}")
    connection = duckdb.connect(
        ":memory:", config={"threads": threads, "memory_limit": memory_limit}
    )
    for table in selected:
        paths = science.parquet_paths(snapshot, manifest, table)
        quoted = ", ".join("'" + path.replace("'", "''") + "'" for path in paths)
        connection.execute(
            f'CREATE VIEW "{table}" AS SELECT * FROM read_parquet([{quoted}], union_by_name=true)'
        )
    return connection, receipt


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2, sort_keys=True))
