#!/usr/bin/env python3
"""One idle, same-selection native context restoration and retry-fence enable. No inference."""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import recover_idle as idle

core = idle.core
ROOT = Path.home() / ".local/state/throttler-recovery-20261111/context-branch-recovery"
HERDR = "/etc/profiles/per-user/notroot/bin/herdr"
IDENTITY = ("pid", "session", "cwd", "provider", "model", "effort", "install", "role")


def owned_json(path):
    stat = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or stat.st_uid != os.getuid()
        or stat.st_mode & 0o777 != 0o600
    ):
        raise RuntimeError("unsafe private receipt")
    body = json.loads(path.read_text())
    if not isinstance(body, dict):
        raise RuntimeError("nonobject private receipt")
    return body


def write_once(path, body):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(body, output)
        output.write("\n")


def loaded(path, pid, version, field="version"):
    if not path.exists():
        return None
    body = owned_json(path)
    if (
        body.get("pid") != pid
        or body.get(field) != version
        or not re.fullmatch(r"[a-f0-9]{64}", body.get("generation", ""))
    ):
        raise RuntimeError("invalid command-specific loaded receipt")
    return body["generation"]


def matching_identity(expected, actual):
    return all(expected[key] == actual.get(key) for key in IDENTITY)


class Recovery:
    def __init__(self, identity, deadline):
        self.identity = identity
        self.deadline = deadline
        self.changes = idle.Changes(
            [ROOT, core.ROOT, Path(identity["transcript"]).parent], deadline
        )

    def run(self, arguments, cap=40):
        return idle.run(arguments, self.deadline, cap=cap)

    def native_identity(self):
        row = json.loads(
            self.run(
                [
                    HERDR,
                    "--session",
                    self.identity["herdrSession"],
                    "agent",
                    "get",
                    self.identity["pane"],
                ]
            )
        )["result"]["agent"]
        if (
            not core.native_started(row, self.identity["transcript"], self.identity["cwd"])
            or row.get("name") != self.identity["name"]
        ):
            raise RuntimeError("native pane identity or idle state changed")
        info = json.loads(
            self.run(
                [
                    HERDR,
                    "--session",
                    self.identity["herdrSession"],
                    "pane",
                    "process-info",
                    "--pane",
                    self.identity["pane"],
                ]
            )
        )["result"]["process_info"]
        foreground = info.get("foreground_processes", [])
        if (
            len(foreground) != 1
            or foreground[0].get("pid") != self.identity["pid"]
            or foreground[0].get("name") != "pi"
        ):
            raise RuntimeError("native process identity changed")
        return row

    def screen(self):
        return self.run(
            [
                HERDR,
                "--session",
                self.identity["herdrSession"],
                "agent",
                "read",
                self.identity["pane"],
                "--source",
                "visible",
                "--lines",
                "300",
                "--format",
                "text",
            ]
        )

    def deliver(self, command):
        if command != "/reload" and not re.fullmatch(
            r"/(?:core-upgrade-state|context-branch-restore|"
            r"compaction-loop-fence-enable) [a-f0-9]{64}",
            command,
        ):
            raise RuntimeError("unsupported native recovery command")
        self.native_identity()
        if not idle.empty_editor(self.screen()):
            raise RuntimeError("native editor is not empty")
        try:
            self.run(
                [
                    idle.HERDR_PROMPT,
                    "-s",
                    self.identity["herdrSession"],
                    self.identity["pane"],
                    command,
                ]
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(
                json.dumps(
                    {"event": "native_delivery_uncertain", "exceptionType": type(error).__name__}
                ),
                file=sys.stderr,
            )
            return False
        return True

    def wait_output(self, text):
        read = json.loads(
            self.run(
                [
                    HERDR,
                    "--session",
                    self.identity["herdrSession"],
                    "pane",
                    "wait-output",
                    self.identity["pane"],
                    "--source",
                    "visible",
                    "--lines",
                    "300",
                    "--match",
                    text,
                    "--timeout",
                    "20000",
                ],
                cap=25,
            )
        )["result"]["read"]
        if read.get("pane_id") != self.identity["pane"] or read.get("source") != "visible":
            raise RuntimeError("reload frame identity changed")
        return read

    def until(self, predicate):
        previous = self.changes.deadline
        self.changes.deadline = min(previous, time.monotonic() + 20)
        try:
            while not (result := predicate()):
                self.changes.wait()
            return result
        finally:
            self.changes.deadline = previous

    def receipt(self, path):
        self.until(path.exists)
        return owned_json(path)

    def request(self, directory, extra=None):
        body = {key: self.identity[key] for key in IDENTITY if key != "session"}
        body.update(
            schema=1,
            nonce=secrets.token_hex(32),
            expires=int(time.time() * 1000) + 240000,
            session=self.identity["transcript"],
        )
        body.update(extra or {})
        write_once(directory / f"{body['nonce']}.request.json", body)
        return body

    def restore(self):
        original = Path(self.identity["transcript"]).read_bytes()
        entries = [json.loads(line) for line in original.splitlines() if line]
        restore = self.request(
            ROOT,
            {
                "expectedLeaf": entries[-1]["id"],
                "targetLeaf": self.identity["targetLeaf"],
                "transcriptSha256": hashlib.sha256(original).hexdigest(),
            },
        )
        self.deliver("/context-branch-restore " + restore["nonce"])
        restored = self.receipt(ROOT / f"{restore['nonce']}.restored.json")
        if (
            restored.get("nonce") != restore["nonce"]
            or restored.get("previousLeaf") != restore["expectedLeaf"]
            or restored.get("targetLeaf") != restore["targetLeaf"]
            or restored.get("originalSha256") != restore["transcriptSha256"]
            or not matching_identity(restore, restored.get("before", {}))
            or not matching_identity(restore, restored.get("after", {}))
        ):
            raise RuntimeError("native restoration proof differs")
        if not Path(self.identity["transcript"]).read_bytes().startswith(original):
            raise RuntimeError("original history prefix changed")
        return {"nonce": restore["nonce"], **restored}

    def execute(self):
        if not self.identity.get("targetLeaf"):
            # Fence-only reload must not replay an unapplied historical selection.
            # Explicit detached-context restoration retains its separate target guard.
            core.require_safe_branch(self.identity["transcript"])
        self.native_identity()
        pid = self.identity["pid"]
        specs = [
            (core.ROOT / f"{pid}.loaded.json", idle.GUARD_VERSION, "guardVersion"),
            (
                core.ROOT / f"{pid}.compaction-fence-loaded.json",
                "prepared-dispatch-fence-v1",
                "version",
            ),
        ]
        if self.identity.get("targetLeaf"):
            specs.append((ROOT / f"{pid}.loaded.json", "context-branch-native-v1", "version"))
        baseline = [loaded(path, pid, version, field) for path, version, field in specs]
        marker = ROOT / (
            hashlib.sha256(
                f"{self.identity['herdrSession']}:{self.identity['pane']}:{pid}".encode()
            ).hexdigest()
            + ".controller-intent.json"
        )
        write_once(
            marker,
            {
                "pid": pid,
                "pane": self.identity["pane"],
                "targetLeaf": self.identity.get("targetLeaf"),
                "inference": False,
            },
        )
        idle.reload_editor_barrier(self.deliver, self.wait_output, self.screen())
        for (path, version, field), old in zip(specs, baseline, strict=True):
            self.until(
                lambda path=path, version=version, field=field, old=old: (
                    (value := loaded(path, pid, version, field)) and value != old
                )
            )
        state_request = self.request(core.ROOT)
        self.deliver("/core-upgrade-state " + state_request["nonce"])
        state = self.receipt(core.ROOT / f"{state_request['nonce']}.state.json")
        if not idle.native_admission(state_request, state):
            raise RuntimeError("fresh native idle identity not proven")
        self.native_identity()
        restored = self.restore() if self.identity.get("targetLeaf") else None
        fence = self.request(core.ROOT)
        self.deliver("/compaction-loop-fence-enable " + fence["nonce"])
        enabled = self.receipt(core.ROOT / f"{fence['nonce']}.compaction-fence.json")
        if (
            enabled.get("nonce") != fence["nonce"]
            or enabled.get("pid") != pid
            or enabled.get("enabled") is not True
            or enabled.get("inference") is not False
            or enabled.get("version") != "prepared-dispatch-fence-v1"
        ):
            raise RuntimeError("native retry fence not proven")
        self.native_identity()
        result = {
            "pid": pid,
            "pane": self.identity["pane"],
            "stateNonce": state_request["nonce"],
            "restoreNonce": restored["nonce"] if restored else None,
            "fenceNonce": fence["nonce"],
            "persistedLeaf": restored["persistedLeaf"] if restored else None,
            "restored": restored is not None,
            "fenceEnabled": True,
            "inference": False,
        }
        write_once(marker.with_suffix(".result.json"), result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--identity-json", type=Path)
    inputs.add_argument("--manifest", type=Path)
    parser.add_argument("--pane")
    parser.add_argument("--target-leaf")
    parser.add_argument("--timeout", type=int, choices=range(1, 301), default=240)
    args = parser.parse_args()
    if args.manifest:
        rows = json.loads(args.manifest.read_text())
        if not isinstance(rows, list):
            raise RuntimeError("identity manifest must be an exact list")
        matches = [row for row in rows if row.get("pane") == args.pane]
        if len(matches) != 1:
            raise RuntimeError("pane is ambiguous or absent in manifest")
        row = matches[0]
        identity = {**row["native"], **{key: row[key] for key in ("herdrSession", "pane", "name")}}
        identity["transcript"] = identity["session"]
        if row.get("contextDetached") and not args.target_leaf:
            raise RuntimeError("detached context requires an explicit reviewed target")
    else:
        identity = owned_json(args.identity_json)
    if args.target_leaf:
        identity["targetLeaf"] = args.target_leaf
    for key in (*IDENTITY, "herdrSession", "pane", "name", "transcript"):
        if key != "session" and not identity.get(key):
            raise RuntimeError("incomplete exact identity")
    identity["session"] = identity["transcript"]
    for directory in [ROOT, core.ROOT]:
        stat = directory.lstat()
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or stat.st_uid != os.getuid()
            or stat.st_mode & 0o777 != 0o700
        ):
            raise RuntimeError("unsafe operational directory")
    lock = idle.acquire_lock(
        core.ROOT
        / (
            hashlib.sha256(f"{identity['herdrSession']}:{identity['pane']}".encode()).hexdigest()
            + ".controller.lock"
        )
    )
    recovery = None
    try:
        recovery = Recovery(identity, time.monotonic() + args.timeout)
        print(json.dumps(recovery.execute()))
    finally:
        if recovery is not None:
            recovery.changes.close()
        os.close(lock)


if __name__ == "__main__":
    main()
