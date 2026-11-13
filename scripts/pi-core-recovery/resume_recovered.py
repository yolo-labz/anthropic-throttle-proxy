#!/usr/bin/env python3
"""Resume one structurally unfinished task once after verified context/fence recovery."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import recover_context as context

PROMPT = (
    "Continue your existing assigned work now from the recovered native context. "
    "Preserve your current provider, model, effort and session; use subscription limits only. "
    "Do not quiesce admission, enable pacing, or invent new work."
)
RECEIPTS = (
    Path.home() / ".local/state/throttler-recovery-20261111/core-upgrade-resume-once-receipts"
)


def unfinished(raw):
    entries = [json.loads(line) for line in raw.splitlines() if line]
    indexed = {}
    for entry in entries:
        if entry.get("type") == "session":
            continue
        if not entry.get("id") or entry["id"] in indexed:
            raise RuntimeError("ambiguous native identity")
        indexed[entry["id"]] = entry
    branch = []
    seen = set()
    leaf = entries[-1]["id"]
    while leaf is not None:
        if leaf in seen or leaf not in indexed:
            raise RuntimeError("ambiguous native branch")
        seen.add(leaf)
        entry = indexed[leaf]
        if entry.get("type") == "message":
            if not isinstance(entry.get("message"), dict):
                raise RuntimeError("invalid native message")
            branch.append(entry)
        leaf = entry.get("parentId")
    if not branch or branch[0]["message"].get("role") not in {"assistant", "toolResult"}:
        raise RuntimeError("no consumed human work")
    assistant = next(
        (entry for entry in branch if entry["message"].get("role") == "assistant"), None
    )
    human = next((entry for entry in branch if entry["message"].get("role") == "user"), None)
    if not assistant or not human or branch.index(assistant) > branch.index(human):
        raise RuntimeError("unconsumed human or absent assistant")
    message = assistant["message"]
    reason = message.get("stopReason")
    if reason not in {"error", "aborted", "toolUse"}:
        raise RuntimeError("completed or uncertain task")
    if re.search(
        r"(?i)policy|privacy|refus|permission|unauthori|no space|ENOSPC|hardware",
        str(message.get("errorMessage", "")),
    ):
        raise RuntimeError("policy hardware or storage hold")
    return {
        "assistantId": assistant["id"],
        "humanId": human["id"],
        "stopReason": reason,
        "leaf": entries[-1]["id"],
    }


def controller_proof(identity, result):
    if (
        result.get("pid") != identity["pid"]
        or result.get("pane") != identity["pane"]
        or result.get("fenceEnabled") is not True
        or result.get("inference") is not False
    ):
        raise RuntimeError("successful recovery proof required")
    nonce = result.get("fenceNonce", "")
    if not re.fullmatch("[a-f0-9]{64}", nonce):
        raise RuntimeError("invalid fence nonce")
    request = context.owned_json(context.core.ROOT / f"{nonce}.request.json")
    receipt = context.owned_json(context.core.ROOT / f"{nonce}.compaction-fence.json")
    if (
        not context.matching_identity(identity, request)
        or receipt.get("nonce") != nonce
        or receipt.get("pid") != identity["pid"]
        or receipt.get("enabled") is not True
        or receipt.get("inference") is not False
        or receipt.get("version") != "prepared-dispatch-fence-v1"
    ):
        raise RuntimeError("exact successful native fence proof required")


def resume_once(recovery, result, directory=RECEIPTS):
    identity = recovery.identity
    controller_proof(identity, result)
    recovery.native_identity()
    if not context.idle.empty_editor(recovery.screen()):
        raise RuntimeError("native editor not empty")
    transcript = Path(identity["transcript"])
    before = transcript.read_bytes()
    disposition = unfinished(before)
    request = recovery.request(context.core.ROOT)
    recovery.deliver("/core-upgrade-state " + request["nonce"])
    state = recovery.receipt(context.core.ROOT / f"{request['nonce']}.state.json")
    if not context.idle.native_admission(request, state):
        raise RuntimeError("fresh native idle identity not proven")
    recovery.native_identity()
    if not context.idle.empty_editor(recovery.screen()) or transcript.read_bytes() != before:
        raise RuntimeError("editor or native branch changed")
    stat = directory.lstat()
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or stat.st_uid != os.getuid()
        or stat.st_mode & 0o777 != 0o700
    ):
        raise RuntimeError("unsafe once receipt directory")
    key = f"{identity['herdrSession']}:{identity['pane']}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    lock = context.idle.acquire_lock(directory / f"{digest}.lock")
    try:
        marker = directory / f"{digest}.attempt.json"
        context.write_once(
            marker,
            {
                "seat": key,
                "pid": identity["pid"],
                "stateNonce": request["nonce"],
                "fenceNonce": result["fenceNonce"],
                "transcriptSha256": hashlib.sha256(before).hexdigest(),
                "expectedModel": identity["model"],
                "expectedEffort": identity["effort"],
                **disposition,
            },
        )
        descriptor = os.open(marker, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            recovery.run(
                [
                    context.idle.HERDR_PROMPT,
                    "-s",
                    identity["herdrSession"],
                    identity["pane"],
                    PROMPT,
                ]
            )
            outcome = "submitted_once"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            outcome = "uncertain_no_resend"
        output = {
            "seat": key,
            "pid": identity["pid"],
            "disposition": outcome,
            "stateNonce": request["nonce"],
            "provider": identity["provider"],
            "model": identity["model"],
            "effort": identity["effort"],
        }
        context.write_once(directory / f"{digest}.recovered-result.json", output)
        return output
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-json", type=Path, required=True)
    parser.add_argument("--controller-result", type=Path, required=True)
    args = parser.parse_args()
    identity = context.owned_json(args.identity_json)
    identity["session"] = identity["transcript"]
    result = context.owned_json(args.controller_result)
    digest = hashlib.sha256(f"{identity['herdrSession']}:{identity['pane']}".encode()).hexdigest()
    lock = context.idle.acquire_lock(context.core.ROOT / f"{digest}.controller.lock")
    recovery = None
    try:
        recovery = context.Recovery(identity, time.monotonic() + 120)
        print(json.dumps(resume_once(recovery, result)))
    finally:
        if recovery is not None:
            recovery.changes.close()
        os.close(lock)


if __name__ == "__main__":
    main()
