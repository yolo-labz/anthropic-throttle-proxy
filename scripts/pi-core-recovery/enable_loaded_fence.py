#!/usr/bin/env python3
"""Enroll one already-loaded native fence; retain every history-settlement hold."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import recover_context as recovery

VERSION = "prepared-dispatch-fence-v1"
FINGERPRINTS = {
    "babe3991f81701aec3b19362e57bb1612dd44069d2bf66193072bab8692650a3:b1d5b11efe4c997e85c80d7df7f18b84f0323cbd68a069b6aaafa8a922ddeafd",
    "b4412035114f516e690e51403035012b21f9d610f5c00d802831b3a740033e03:f51e78ef1da66b5803fc91e185f07b968f8f989ab9326b7744958416aad3f6f6",
}


def source_fingerprint(install):
    root = Path(install) / "dist/core"
    fingerprint = ":".join(
        hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("agent-session.js", "compaction/compaction.js")
    )
    if fingerprint not in FINGERPRINTS:
        raise RuntimeError("unreviewed native source fingerprint")
    return fingerprint


def qualification(before_path, refusal_path):
    before = recovery.owned_json(before_path)
    identity = before["identity"]
    if identity.get("targetLeaf") or any(
        not identity.get(key)
        for key in (*recovery.IDENTITY, "transcript", "pane", "name", "herdrSession")
    ):
        raise RuntimeError("incomplete exact fence identity")
    if identity["session"] != identity["transcript"]:
        raise RuntimeError("different session identities")
    refusal = recovery.owned_json(refusal_path)
    actual = refusal.get("actual", {})
    if (
        refusal.get("command") != "core-upgrade-state"
        or refusal.get("reason") != "native-context-mismatch"
        or not recovery.matching_identity(identity, actual)
        or actual.get("nativeContext", {}).get("agrees") is not False
    ):
        raise RuntimeError("not the exact failed history-settlement scope")
    return before, identity


def enable(operation, before_path, refusal_path, directory):
    before, identity = qualification(before_path, refusal_path)
    if operation.identity != identity:
        raise RuntimeError("operation identity differs")
    fingerprint = source_fingerprint(identity["install"])
    pid = identity["pid"]
    loaded_path = recovery.core.ROOT / f"{pid}.compaction-fence-loaded.json"
    generation = recovery.loaded(loaded_path, pid, VERSION)
    if not generation or loaded_path.stat().st_mtime_ns <= before_path.stat().st_mtime_ns:
        raise RuntimeError("missing or stale fence-specific loaded proof")
    operation.native_identity()
    if not recovery.idle.empty_editor(operation.screen()):
        raise RuntimeError("native editor is not empty")
    # The native handler independently reobserves pin, selection, role, idle and queue.
    # No STATE, reload, branch navigation, shutdown or inference is issued here.
    marker = (
        directory
        / f"{identity['herdrSession']}-{identity['pane'].replace(':', '-')}-{pid}.intent.json"
    )
    request = operation.request(recovery.core.ROOT)
    recovery.write_once(
        marker,
        {
            "identity": identity,
            "nonce": request["nonce"],
            "loadedGeneration": generation,
            "sourceFingerprint": fingerprint,
            "failedBaseline": str(before_path),
            "refusal": str(refusal_path),
            "historySettlementHold": True,
            "prefixSha256": before["prefixSha256"],
            "inference": False,
        },
    )
    operation.deliver("/compaction-loop-fence-enable " + request["nonce"])
    receipt = operation.receipt(recovery.core.ROOT / f"{request['nonce']}.compaction-fence.json")
    if (
        receipt.get("nonce") != request["nonce"]
        or receipt.get("pid") != pid
        or receipt.get("version") != VERSION
        or receipt.get("fingerprint") != fingerprint
        or receipt.get("enabled") is not True
        or receipt.get("inference") is not False
    ):
        raise RuntimeError("exact native fence enable not proven; retain intent")
    result = {
        "pid": pid,
        "pane": identity["pane"],
        "fenceEnabled": True,
        "nonce": request["nonce"],
        "historySettlementHold": True,
        "contextAgreementClaimed": False,
        "inference": False,
    }
    recovery.write_once(marker.with_suffix(".result.json"), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--refusal", required=True, type=Path)
    args = parser.parse_args()
    _, identity = qualification(args.before, args.refusal)
    directory = recovery.core.ROOT.parent / "direct-loaded-fence"
    directory.mkdir(mode=0o700, exist_ok=True)
    stat = directory.lstat()
    if directory.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o700:
        raise RuntimeError("unsafe intent directory")
    key = hashlib.sha256(f"{identity['herdrSession']}:{identity['pane']}".encode()).hexdigest()
    lock = recovery.idle.acquire_lock(recovery.core.ROOT / (key + ".controller.lock"))
    operation = None
    try:
        operation = recovery.Recovery(identity, time.monotonic() + 120)
        print(json.dumps(enable(operation, args.before, args.refusal, directory)))
    finally:
        if operation:
            operation.changes.close()
        os.close(lock)


if __name__ == "__main__":
    main()
