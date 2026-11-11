#!/usr/bin/env python3
"""Export one idle native memory branch once; never import, reconcile, or infer."""

import argparse
import hashlib
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import time
from pathlib import Path

import enable_loaded_fence as fence
import recover_context as context


def digest(value):
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def entries(raw):
    rows = [json.loads(line) for line in raw.splitlines() if line]
    if not rows or rows[0].get("type") != "session":
        raise RuntimeError("missing session header")
    indexed = {}
    for row in rows[1:]:
        if (
            not row.get("id")
            or row["id"] in indexed
            or not row.get("type")
            or row.get("type") == "session"
            or "parentId" not in row
        ):
            raise RuntimeError("ambiguous raw entry identity")
        indexed[row["id"]] = row
    return rows, indexed


def disk_branch(indexed, leaf):
    branch = []
    seen = set()
    while leaf is not None:
        if leaf in seen or leaf not in indexed:
            raise RuntimeError("invalid original disk ancestry")
        seen.add(leaf)
        row = indexed[leaf]
        branch.append(row)
        leaf = row.get("parentId")
    return branch[::-1]


def verify_export(original, exported, identity, expected):
    disk, indexed = entries(original)
    rows, exported_index = entries(exported)
    if rows[0].get("id") != disk[0].get("id") or rows[0].get("cwd") != identity["cwd"]:
        raise RuntimeError("exported session header differs")
    branch = rows[1:]
    for index, row in enumerate(branch):
        if row.get("parentId") != (branch[index - 1]["id"] if index else None):
            raise RuntimeError("export is not a complete linear native branch")
    ids = [row["id"] for row in branch]
    work = [row["id"] for row in branch if row.get("type") in {"message", "compaction"}]
    if (len(ids), digest(ids), len(work), digest(work)) != (
        expected["branchEntries"],
        expected["branchIdDigest"],
        expected["workEntries"],
        expected["workIdDigest"],
    ):
        raise RuntimeError("native branch advanced or differs; retain artifact, do not resend")
    try:
        original_active = disk_branch(indexed, disk[-1]["id"])
        ancestry_error = None
    except RuntimeError as error:
        original_active = None
        ancestry_error = str(error)
    # Even a damaged active ancestry retains raw work worth comparing with the memory export.
    original_work = [row for row in disk[1:] if row.get("type") in {"message", "compaction"}]
    missing = [row["id"] for row in original_work if row["id"] not in exported_index]
    # External reparenting may alter parentId; every other shared raw field must agree.
    conflicting = [
        row["id"]
        for row in original_work
        if row["id"] in exported_index
        and {k: v for k, v in row.items() if k != "parentId"}
        != {k: v for k, v in exported_index[row["id"]].items() if k != "parentId"}
    ]
    return {
        "branchEntries": len(ids),
        "branchIdDigest": digest(ids),
        "entryTypeDigest": digest([[row["id"], row["type"]] for row in branch]),
        "workEntries": len(work),
        "workIdDigest": digest(work),
        "diskAncestryValid": original_active is not None,
        "diskAncestryError": ancestry_error,
        "originalDiskActiveWork": sum(
            row.get("type") in {"message", "compaction"} for row in original_active
        )
        if original_active is not None
        else None,
        "originalDiskRawWork": len(original_work),
        "diskWorkComparisonScope": "all-raw-work-excluding-parentId",
        "missingDiskWorkIds": missing,
        "conflictingDiskWorkIds": conflicting,
        "sharedRawContentMatches": not conflicting,
        "nativeRawContentEqualityClaimed": False,
        "historySettlementHold": True,
        "completeMemorySnapshot": True,
    }


def bootstrap_guard(identity):
    raw = Path(f"/proc/{identity['pid']}/environ").read_bytes()
    environment = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    for variable, key in (
        ("PI_FLEET_INSTALL", "install"),
        ("PI_ROLE_RESOLVED", "role"),
        ("HERDR_AGENT_NAME", "name"),
        ("HERDR_SESSION", "herdrSession"),
        ("HERDR_PANE_ID", "pane"),
    ):
        if environment.get(variable.encode()) != str(identity[key]).encode():
            raise RuntimeError("native bootstrap identity changed")


def private_directory(directory):
    if not directory.is_absolute() or ".." in directory.parts or re.search(r"\s", str(directory)):
        raise RuntimeError("unsafe private absolute export directory")
    for part in (directory, *directory.parents):
        if part.is_symlink():
            raise RuntimeError("symlink export directory ancestor")
    stat = directory.lstat()
    if not directory.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o700:
        raise RuntimeError("unsafe private absolute export directory")


def deliver_export(operation, artifact):
    identity = operation.identity
    try:
        operation.run(
            [
                context.idle.HERDR_PROMPT,
                "-s",
                identity["herdrSession"],
                identity["pane"],
                f"/export {artifact}",
            ]
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"deliveryUncertain": type(error).__name__}), file=sys.stderr)
        return False
    return True


def fresh_native_state(operation, original, baseline):
    request = operation.request(context.core.ROOT)
    operation.deliver("/core-upgrade-state " + request["nonce"])
    refusal = operation.receipt(context.core.ROOT / f"{request['nonce']}.refused.json")
    actual = refusal.get("actual", {})
    native = actual.get("nativeContext", {})
    if (
        refusal.get("nonce") != request["nonce"]
        or refusal.get("command") != "core-upgrade-state"
        or refusal.get("reason") != "native-context-mismatch"
        or not context.matching_identity(operation.identity, actual)
        or actual.get("idle") is not True
        or actual.get("pending") is not False
        or actual.get("editorBytes") != 0
        or native.get("agrees") is not False
        or native.get("transcriptSha256") != hashlib.sha256(original).hexdigest()
    ):
        raise RuntimeError("fresh native idle, queue and pin not proven")
    for key in ("branchEntries", "branchIdDigest", "workEntries", "workIdDigest"):
        if native.get(key) != baseline.get(key):
            raise RuntimeError("native branch advanced since baseline; hold without export")
    return request["nonce"], native


def closed_artifact(events, name):
    offset = 0
    closed = False
    while offset < len(events):
        _, mask, _, length = struct.unpack_from("iIII", events, offset)
        offset += 16
        filename = events[offset : offset + length].split(b"\0", 1)[0]
        offset += length
        if mask & 0x4000:
            raise RuntimeError("filesystem completion events overflowed")
        closed = closed or bool(mask & 0x8 and filename == os.fsencode(name))
    return closed


def wait_closed(changes, artifact):
    while True:
        remaining = max(0, changes.deadline - time.monotonic())
        if not context.idle.select.select([changes.fd], [], [], remaining)[0]:
            raise TimeoutError("native export close-write completion not observed")
        if closed_artifact(os.read(changes.fd, 65536), artifact.name):
            return


def preexport_completion(marker, identity, original):
    previous = context.owned_json(marker)
    outcome_path = marker.with_suffix(".result.json")
    outcome = context.owned_json(outcome_path)
    old_artifact = Path(previous["artifact"])
    if (
        previous.get("identity") != identity
        or previous.get("originalSha256") != hashlib.sha256(original).hexdigest()
        or previous.get("inference") is not False
        or outcome.get("held") is not True
        or outcome.get("reason") != "native branch advanced since baseline; hold without export"
        or outcome.get("pid") != identity["pid"]
        or outcome.get("artifact") != str(old_artifact)
        or outcome.get("inference") is not False
        or outcome.get("resendPermitted") is not False
        or old_artifact.exists()
        or old_artifact.is_symlink()
    ):
        raise RuntimeError("not a proven pre-export-only hold")
    return {
        "previousIntentSha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
        "previousResultSha256": hashlib.sha256(outcome_path.read_bytes()).hexdigest(),
        "previousNativeContext": previous["expectedNativeContext"],
    }


def export_once(operation, expected, directory, complete_preexport_hold=False):
    private_directory(directory)
    identity = operation.identity
    operation.native_identity()
    bootstrap_guard(identity)
    if not context.idle.empty_editor(operation.screen()):
        raise RuntimeError("native editor is not empty")
    transcript = Path(identity["transcript"])
    original = transcript.read_bytes()
    nonce = secrets.token_hex(32)
    artifact = directory / f"{nonce}.jsonl"
    key = digest([identity["herdrSession"], identity["pane"], identity["pid"]])
    marker = context.core.ROOT / f"{key}.memory-export-intent.json"
    completion = (
        preexport_completion(marker, identity, original) if complete_preexport_hold else None
    )
    if completion:
        marker = context.core.ROOT / f"{key}.memory-export-preexport-completion-intent.json"
    context.write_once(
        marker,
        {
            "identity": identity,
            "artifact": str(artifact),
            "originalSha256": hashlib.sha256(original).hexdigest(),
            "expectedNativeContext": expected,
            "inference": False,
            **({"preexportCompletion": completion} if completion else {}),
        },
    )
    descriptor = os.open(marker, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    changes = context.idle.Changes([directory], operation.deadline)
    try:
        state_nonce, expected = fresh_native_state(operation, original, expected)
        operation.native_identity()
        bootstrap_guard(identity)
        if not context.idle.empty_editor(operation.screen()):
            raise RuntimeError("native editor is not empty")
        if transcript.read_bytes() != original or artifact.exists() or artifact.is_symlink():
            raise RuntimeError("history advanced or destination exists; no export sent")
        delivered = deliver_export(operation, artifact)
        wait_closed(changes, artifact)
        snapshot = artifact.read_bytes()
        report = verify_export(original, snapshot, identity, expected)
        if completion:
            old = completion["previousNativeContext"]
            work = [
                row["id"]
                for row in entries(snapshot)[0][1:]
                if row.get("type") in {"message", "compaction"}
            ]
            if (
                len(work) < old["workEntries"]
                or digest(work[: old["workEntries"]]) != old["workIdDigest"]
            ):
                raise RuntimeError("previous native work prefix missing; retain artifact and hold")
            report["previousNativeWorkPrefixRetained"] = True
        stat = artifact.lstat()
        if artifact.is_symlink() or stat.st_uid != os.getuid() or not artifact.is_file():
            raise RuntimeError("export artifact is not private")
        artifact.chmod(0o600)
        operation.native_identity()
        bootstrap_guard(identity)
        if not context.idle.empty_editor(operation.screen()):
            raise RuntimeError("native editor is not empty")
        if transcript.read_bytes() != original:
            raise RuntimeError("original transcript advanced; retain artifact and hold")
        result = {
            **report,
            "artifact": str(artifact),
            "snapshotSha256": hashlib.sha256(snapshot).hexdigest(),
            "originalSha256": hashlib.sha256(original).hexdigest(),
            "selectedPin": {key: identity[key] for key in ("provider", "model", "effort")},
            "pid": identity["pid"],
            "inference": False,
            "originalUnchanged": True,
            "deliveryReportedSuccess": delivered,
            "stateNonce": state_nonce,
            "selectionProof": "fresh-native-state-refusal",
            "nativeExportCompletionObserved": "targeted-inotify-close-write",
        }
        context.write_once(marker.with_suffix(".result.json"), result)
        return result
    except (RuntimeError, ValueError, TimeoutError, OSError, subprocess.SubprocessError) as error:
        context.write_once(
            marker.with_suffix(".result.json"),
            {
                "held": True,
                "reason": str(error),
                "artifact": str(artifact),
                "pid": identity["pid"],
                "inference": False,
                "resendPermitted": False,
            },
        )
        raise
    finally:
        changes.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--refusal", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--complete-preexport-hold", action="store_true")
    args = parser.parse_args()
    _, identity = fence.qualification(args.before, args.refusal)
    loaded = context.core.ROOT / f"{identity['pid']}.loaded.json"
    context.loaded(loaded, identity["pid"], context.idle.GUARD_VERSION, "guardVersion")
    if not loaded.exists() or loaded.stat().st_mtime_ns > args.refusal.stat().st_mtime_ns:
        raise RuntimeError("guard was reloaded since accepted mismatch proof")
    expected = context.owned_json(args.refusal)["actual"]["nativeContext"]
    args.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    private_directory(args.directory)
    os.umask(0o077)
    key = hashlib.sha256(f"{identity['herdrSession']}:{identity['pane']}".encode()).hexdigest()
    lock = context.idle.acquire_lock(context.core.ROOT / (key + ".controller.lock"))
    operation = None
    try:
        operation = context.Recovery(identity, time.monotonic() + 90)
        print(
            json.dumps(
                export_once(operation, expected, args.directory, args.complete_preexport_hold)
            )
        )
    finally:
        if operation:
            operation.changes.close()
        os.close(lock)


if __name__ == "__main__":
    main()
