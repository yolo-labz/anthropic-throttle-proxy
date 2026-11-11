#!/usr/bin/env python3
"""Plan/launch only the reviewed stopped Horaco incident; never send a task prompt."""

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import recover_core as core

NONCE = "c29474d25370110b51ccff686581e1542f9f68363b343300a2f3f37b8c8f9148"
SOURCE = Path("/nix/store/qdadmldzx838xclg0hyshjdlcpi02nhq-source")
INSTALL = Path.home() / ".local/share/pi-fleet/1.5.2-83a4dd90"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def original_snapshot(evidence, raw):
    repaired = evidence.get("externalParentRepair", False)
    if repaired:
        lines = raw.splitlines(keepends=True)
        prefix = b"".join(lines[: evidence["normalizedPrefixRows"]])
        if sha(prefix) != evidence["normalizedPrefixSha256"]:
            raise RuntimeError("normalized original prefix changed")
        # Native JSON.stringify is required: Python float formatting changes source bytes.
        result = subprocess.run(
            [
                "/run/current-system/sw/bin/node",
                "--input-type=module",
                "-e",
                "let b='';for await(const c of process.stdin)b+=c;"
                "const q=JSON.parse(b);process.stdout.write(q.lines.map(line=>{"
                "const e=JSON.parse(line);return q.ids.includes(e.id)?JSON.stringify(e):line"
                "}).join('\\n')+'\\n');",
            ],
            input=json.dumps(
                {
                    "lines": [
                        line.decode().rstrip("\n")
                        for line in lines[: evidence["normalizedPrefixRows"]]
                    ],
                    "ids": evidence["normalizedCompactIds"],
                }
            ).encode(),
            capture_output=True,
            check=True,
            timeout=10,
        )
        reconstructed = result.stdout
        if (
            len(reconstructed) != evidence["originalBytes"]
            or sha(reconstructed) != evidence["originalPrefixSha256"]
        ):
            raise RuntimeError("original record reconstruction differs")
    else:
        prefix = raw[: evidence["originalBytes"]]
    if not repaired and sha(prefix) != evidence["originalPrefixSha256"]:
        raise RuntimeError("original prefix changed")
    entries = [json.loads(line) for line in prefix.splitlines() if line]
    index = {entry["id"]: entry for entry in entries if "id" in entry}
    if len(index) != sum("id" in entry for entry in entries):
        raise RuntimeError("duplicate original identity")
    current = next(reversed(index))
    if current != "74edcb64" or index[current]["type"] != "context_edit":
        raise RuntimeError("original context-edit target changed")
    seen = set()
    branch = []
    while current is not None:
        if current in seen or current not in index:
            raise RuntimeError("original branch ambiguous")
        seen.add(current)
        branch.append(index[current])
        current = index[current].get("parentId")
    work = [entry["id"] for entry in branch if entry["type"] in {"message", "compaction"}]
    if work != evidence["originalBranchWorkIds"] or len(work) != 10:
        raise RuntimeError("original ten work entries changed")
    suffix = [json.loads(line) for line in raw[len(prefix) :].splitlines() if line]
    expected = [("c5582014", None if repaired else "1fe7721e"), ("a4bbb629", "c5582014")]
    if [(entry.get("id"), entry.get("parentId")) for entry in suffix] != expected or any(
        entry.get("type") != "custom" or entry.get("customType") != "pi-artifact-cleanup"
        for entry in suffix
    ):
        raise RuntimeError("incident suffix changed")
    if "1fe7721e" in index:
        raise RuntimeError("incident parent is not missing")
    return prefix


def owned_json(path, modes=(0o600,)):
    metadata = path.lstat()
    if (
        metadata.st_uid != os.getuid()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) not in modes
    ):
        raise RuntimeError("unsafe incident evidence")
    return json.loads(path.read_text())


def durable_once(path, data, equal_ok=False):
    if os.path.lexists(path):
        if equal_ok and owned_json(path) == data:
            return
        raise RuntimeError("existing incident intent or differing plan; inspect, never resend")
    with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump(data, output)
        output.flush()
        os.fsync(output.fileno())
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_frozen_bundle():
    bundle = Path(__file__).resolve().parents[3]
    allowed = Path.home() / ".local/state/throttler-recovery-20261111/dangling-tail-candidates"
    if bundle.parent != allowed:
        raise RuntimeError("execution requires reviewed content-addressed source bundle")
    manifest = owned_json(bundle / "manifest.json", modes=(0o444,))
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if sha(encoded) != bundle.name:
        raise RuntimeError("source bundle identity differs")
    for relative, expected in manifest.items():
        path = bundle / relative
        if path.resolve().is_relative_to(bundle) is False or core.fingerprint(path) != expected:
            raise RuntimeError("frozen source bundle changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--external-parent-repair", action="store_true", required=True)
    parser.add_argument("--reviewed-shell-screen-sha256", required=True)
    args = parser.parse_args()
    root = Path.home() / ".local/state/throttler-recovery-20261111"
    directory = root / (
        "dangling-tail-recovery-v2" if args.external_parent_repair else "dangling-tail-recovery"
    )
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if (
        metadata.st_uid != os.getuid()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError("unsafe incident directory")
    if os.path.lexists(directory / "launch-intent.json"):
        raise RuntimeError(
            "prior launch intent exists; inspect without preparing or launching again"
        )
    evidence = next(
        row
        for row in owned_json(root / "compaction-priority-next2-intent.json", modes=(0o600, 0o644))
        if row["pane"] == "w3F:p1"
    )
    request = owned_json(core.ROOT / f"{NONCE}.request.json")
    ready = owned_json(core.ROOT / f"{NONCE}.ready.json")
    expected = {
        "pid": 236202,
        "shellPid": 232320,
        "herdrSession": "home",
        "pane": "w3F:p1",
        "name": "horaco-orch",
        "provider": "claude-lane",
        "model": "claude-opus-5-5",
        "effort": "max",
        "role": "orch",
    }
    if any(request.get(key) != value or ready.get(key) != value for key, value in expected.items()):
        raise RuntimeError("prequit native proof differs")
    if any(ready.get(key) != value for key, value in request.items()):
        raise RuntimeError("prequit ready mismatches qualified bootstrap")
    if ready["nativeBootstrap"]["hash"] != request["expectedBootstrapHash"]:
        raise RuntimeError("native bootstrap hash changed")
    if request["nativeArgs"] != ready["nativeBootstrap"]["replayArgs"]:
        raise RuntimeError("native bootstrap arguments changed")
    if Path(f"/proc/{request['pid']}").exists() or not core.shell_ready(
        core.info("home", "w3F:p1"), request["pid"], request["shellPid"], request["cwd"]
    ):
        raise RuntimeError("original shell-only boundary not proven")
    screen = core.call(
        "herdr",
        "--session",
        "home",
        "pane",
        "read",
        "w3F:p1",
        "--source",
        "visible",
        "--lines",
        "20",
        "--format",
        "text",
    )
    if sha(screen.encode()) != args.reviewed_shell_screen_sha256:
        raise RuntimeError("operator-reviewed empty shell screen changed")
    raw = Path(request["session"]).read_bytes()
    if args.external_parent_repair:
        if sha(raw) != "3d6417321aae8f7f4bfc6da1548787981fcaf58efa0dc18b47d530c9478f3dae":
            raise RuntimeError("reviewed external repair snapshot differs")
        evidence = {
            **evidence,
            "externalParentRepair": True,
            "normalizedPrefixRows": 1854,
            "normalizedPrefixSha256": (
                "7a0e27fed1fbbdeb6b1e62c590938e47e09a298d3dc545d85577050c29287313"
            ),
            "normalizedCompactIds": ["e5db6de6", "1b72248e", "e8e8b70f", "dea3e27d", "45e78986"],
        }
    original_snapshot(evidence, raw)
    pin = json.loads((SOURCE / "packaging/fleet/fleet-pin.x86_64-linux.json").read_text())
    if json.loads((INSTALL / ".fleet-pin.json").read_text()) != pin:
        raise RuntimeError("immutable accepted source pin differs")
    core.call(
        "bash", str(SOURCE / "packaging/fleet/fleet-install.sh"), "verify", "--source", str(INSTALL)
    )
    launcher = request["launcher"]
    if core.fingerprint(launcher) != request["launcherSha256"] or not launcher.startswith(
        "/nix/store/"
    ):
        raise RuntimeError("immutable original launcher changed")
    if (Path(request["launchRoot"]) / "current").resolve(strict=True) != INSTALL.resolve(
        strict=True
    ):
        raise RuntimeError("immutable launch root differs")
    extension = str(Path(__file__).with_name("restore.ts").resolve(strict=True))
    command = shlex.join(
        [
            core.launcher_tool(launcher, "env"),
            f"PI_FLEET_ROOT={request['launchRoot']}",
            f"PI_FLEET_NODE={request['nativeNode']}",
            *[f"{key}={value}" for key, value in request["bootstrapEnv"].items()],
            str(Path(request["launchRoot"]) / "fleet-wrapper.sh"),
            *request["nativeArgs"],
            "--extension",
            extension,
            "--session",
            request["session"],
            "--model",
            "claude-lane/claude-opus-5-5",
            "--thinking",
            "max",
        ]
    )
    plan = {
        **{
            key: evidence[key]
            for key in [
                "externalParentRepair",
                "normalizedPrefixRows",
                "normalizedPrefixSha256",
                "normalizedCompactIds",
            ]
            if key in evidence
        },
        "incident": "home-w3f-p1-20261111",
        "originalPrefixSha256": evidence["originalPrefixSha256"],
        "originalBytes": evidence["originalBytes"],
        "originalBranchWorkIds": evidence["originalBranchWorkIds"],
        "targetLeaf": "74edcb64",
        "postLaunchRegistration": {
            "after": (
                "fresh new PID/native STATE proves install/session/cwd/Opus/max/orch/bootstrap"
            ),
            "command": ["herdr", "--session", "home", "agent", "rename", "w3F:p1", "horaco-orch"],
            "verify": "native agent get name=horaco-orch, original session/cwd and same new PID",
            "automatic": False,
        },
        "beforeSha256": sha(raw),
        "extensionSha256": core.fingerprint(extension),
        "shellScreenSha256": args.reviewed_shell_screen_sha256,
        "command": command,
    }
    durable_once(directory / "launch-plan.json", plan, equal_ok=True)
    if args.execute:
        verify_frozen_bundle()
        if Path(request["session"]).read_bytes() != raw or not core.shell_ready(
            core.info("home", "w3F:p1"), request["pid"], request["shellPid"], request["cwd"]
        ):
            raise RuntimeError("incident boundary changed before launch")
        if (
            sha(
                core.call(
                    "herdr",
                    "--session",
                    "home",
                    "pane",
                    "read",
                    "w3F:p1",
                    "--source",
                    "visible",
                    "--lines",
                    "20",
                    "--format",
                    "text",
                ).encode()
            )
            != args.reviewed_shell_screen_sha256
        ):
            raise RuntimeError("reviewed shell editor changed")
        if core.fingerprint(extension) != plan["extensionSha256"]:
            raise RuntimeError("reviewed extension changed before launch")
        durable_once(
            directory / "launch-intent.json",
            {
                "incident": plan["incident"],
                "planSha256": core.fingerprint(directory / "launch-plan.json"),
            },
        )
        verify_frozen_bundle()
        # One dispatch only: uncertainty must be inspected, never retried by this helper.
        core.call("herdr", "--session", "home", "pane", "run", "w3F:p1", command)
    print(
        json.dumps(
            {
                "plan": str(directory / "launch-plan.json"),
                "launchDispatched": args.execute,
                "inference": False,
                "restorationAutomatic": False,
            }
        )
    )


if __name__ == "__main__":
    main()
