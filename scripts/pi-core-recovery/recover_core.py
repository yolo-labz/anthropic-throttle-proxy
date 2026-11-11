#!/usr/bin/env python3
"""Explicit, bounded idle-only native core replacement; never sends model prompts."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / ".local/state/throttler-recovery-20261111/core-upgrades"
LAUNCHER = Path("/etc/profiles/per-user/notroot/bin/pi")


async def _call(executable, parameters):
    if executable == "herdr":
        process = await asyncio.create_subprocess_exec(
            "/etc/profiles/per-user/notroot/bin/herdr",
            *parameters,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    elif executable == "bash":
        process = await asyncio.create_subprocess_exec(
            "/run/current-system/sw/bin/bash",
            *parameters,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        raise ValueError("unsupported operational executable")
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=35)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, executable, stderr=stderr)
    return stdout.decode()


def call(executable, *parameters):
    return asyncio.run(_call(executable, parameters))


def info(session, pane):
    return json.loads(call("herdr", "--session", session, "pane", "process-info", "--pane", pane))[
        "result"
    ]["process_info"]


def agent(session, pane):
    return json.loads(call("herdr", "--session", session, "agent", "get", pane))["result"]["agent"]


def shell_ready(process_info, old_pid, shell_pid, cwd):
    foreground = process_info.get("foreground_processes", [])
    return (
        process_info.get("shell_pid") == shell_pid
        and len(foreground) == 1
        and foreground[0].get("pid") == shell_pid
        and foreground[0].get("cwd") == cwd
        and foreground[0].get("pid") != old_pid
        and foreground[0].get("name") in {"bash", "zsh", "fish"}
    )


def native_started(state, transcript, cwd):
    identity = state.get("agent_session")
    return (
        state.get("agent") == "pi"
        and state.get("agent_status") in {"idle", "done"}
        and isinstance(identity, dict)
        and identity.get("source") == "herdr:pi"
        and identity.get("value") == transcript
        and state.get("cwd") == cwd
    )


def dispatch_and_detect(args, command):
    try:
        call("herdr", "--session", args.session, "pane", "run", args.pane, command)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, TimeoutError) as error:
        # An uncertain response can follow a successful launch. Never dispatch twice.
        print(
            json.dumps(
                {
                    "event": "native_launch_uncertain",
                    "exceptionType": type(error).__name__,
                    "returncode": getattr(error, "returncode", None),
                }
            ),
            file=sys.stderr,
        )
        return detect_native_start(args)
    return detect_native_start(args)


def detect_native_start(args):
    # Detection may lag pane.run. One bounded wait, without launching again.
    deadline = time.monotonic() + 30
    detection_error = None
    while True:
        try:
            state = agent(args.session, args.pane)
            if native_started(state, args.transcript, args.cwd):
                break
        except subprocess.CalledProcessError as error:
            detection_error = error
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "launched core detection did not settle; never retry launch blindly"
            ) from detection_error
        time.sleep(0.2)


def start_registered_repair(args, request, launcher):
    """Repair a known identity through native preregistration."""
    marker = ROOT / f"{args.nonce}.{args.phase}.shell-ready"
    env_binary = launcher_tool(launcher, "env")
    readlink_binary = shlex.quote(launcher_tool(launcher, "readlink"))
    exact = shlex.join(
        [
            env_binary,
            f"PI_FLEET_ROOT={request['launchRoot']}",
            f"PI_ROLE={request['targetRole']}",
            launcher,
        ]
    )
    external_command = (
        (
            '[[ $(whence -w pi) == "pi: command" || ${aliases[pi]-} == "nocorrect pi" ]] '
            "&& (( ! ${+functions[pi]} && ! ${+dis_functions[pi]} ))"
        )
        if request["shellName"] == "zsh"
        else '[ "$(type -t pi)" = file ]'
    )
    command_path = "whence -p pi" if request["shellName"] == "zsh" else "command -v pi"
    # Refuse preexisting aliases/functions. Only our own one-shot function is removed.
    line = (
        f"if {external_command} && "
        f'[ "$({readlink_binary} -f -- "$({command_path})")" = {shlex.quote(launcher)} ]; then '
        f'function pi() {{ unset -f pi; {exact} "$@"; }}; '
        f"(umask 077; printf ready > {shlex.quote(str(marker))}); "
        f"else (umask 077; printf blocked > {shlex.quote(str(marker))}); fi"
    )
    call("herdr", "--session", args.session, "pane", "run", args.pane, line)
    deadline = time.monotonic() + 10
    while not marker.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("one-shot native launch binding was not acknowledged")
        time.sleep(0.1)
    if marker.read_text() != "ready":
        raise RuntimeError("original shell has a custom pi alias/function or is not ready")
    # The receipt can become visible while its short writing subshell still exists.
    while not shell_ready(
        info(args.session, args.pane), request["pid"], request["shellPid"], args.cwd
    ):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "one-shot function armed but original shell unsettled; inspect before continuing"
            )
        time.sleep(0.1)
    call(
        "herdr",
        "--session",
        args.session,
        "agent",
        "start",
        args.name,
        "--kind",
        "pi",
        "--pane",
        args.pane,
        "--timeout",
        "30000",
        "--",
        "--session",
        args.transcript,
        "--model",
        f"{args.provider}/{args.model}",
        "--thinking",
        args.effort,
    )


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def disk_context(path):
    """Metadata-only physical-tail proof, matching the synchronous native guard."""
    raw = Path(path).read_bytes()
    entries = [json.loads(line) for line in raw.splitlines() if line]
    indexed = {}
    for entry in entries:
        if entry.get("type") == "session":
            continue
        if not entry.get("id") or entry["id"] in indexed:
            raise RuntimeError("ambiguous disk context")
        indexed[entry["id"]] = entry
    leaf = next(reversed(indexed), None)
    branch = []
    seen = set()
    current = leaf
    while current is not None:
        if current in seen or current not in indexed:
            raise RuntimeError("ambiguous disk context")
        seen.add(current)
        entry = indexed[current]
        branch.append(entry)
        current = entry.get("parentId")
    branch.reverse()
    work = [entry for entry in branch if entry.get("type") in {"message", "compaction"}]

    def ids_digest(values):
        return hashlib.sha256(
            json.dumps([entry["id"] for entry in values], separators=(",", ":")).encode()
        ).hexdigest()

    branch_digest = ids_digest(branch)
    work_digest = ids_digest(work)
    return {
        "schema": 1,
        "leaf": leaf,
        "branchEntries": len(branch),
        "branchIdDigest": branch_digest,
        "workEntries": len(work),
        "workIdDigest": work_digest,
        "diskLeaf": leaf,
        "diskBranchIdDigest": branch_digest,
        "diskWorkIdDigest": work_digest,
        "transcriptSha256": hashlib.sha256(raw).hexdigest(),
        "agrees": True,
    }


def require_context_agreement(path, native):
    if not isinstance(native, dict) or native != disk_context(path):
        raise RuntimeError("native context does not match exact disk branch; no shutdown")
    return native


def launcher_tool(launcher, name):
    match = re.search(
        r"(/nix/store/[A-Za-z0-9-]+-coreutils-[^:/\"]+/bin)", Path(launcher).read_text()
    )
    if not match:
        raise RuntimeError("immutable launcher has no qualified coreutils input")
    executable = Path(match.group(1)) / name
    if not executable.is_file():
        raise RuntimeError("qualified launcher tool is missing")
    return str(executable)


def require_safe_branch(path):
    """Inspect native metadata only; never expose user/tool payloads."""
    entries = {}
    for line in Path(path).read_text().splitlines():
        entry = json.loads(line)
        if "id" in entry:
            if entry["id"] in entries:
                raise RuntimeError("ambiguous native branch")
            entries[entry["id"]] = entry
    if not entries:
        raise RuntimeError("empty native session")
    current = next(reversed(entries))
    seen = set()
    branch = []
    while current is not None:
        if current in seen or current not in entries:
            raise RuntimeError("ambiguous native branch")
        seen.add(current)
        entry = entries[current]
        branch.append(entry)
        if entry.get("type") == "custom" and entry.get("customType") == "context-recovery":
            raise RuntimeError("historical startup target requires separate review")
        current = entry.get("parentId")
    work_types = {"message", "compaction"}
    if not any(entry.get("type") in work_types for entry in branch) and any(
        entry.get("type") in work_types for entry in entries.values()
    ):
        raise RuntimeError("metadata-only branch would displace existing work context")
    target = next(
        (
            entry
            for entry in branch
            if entry.get("type") == "custom"
            and entry.get("customType") == "cross-family-model-target"
        ),
        None,
    )
    if target is not None:
        # Installed parity session_start consumes only its newest target, once.
        codex = {"openai-codex", "codex-a", "codex-b", "codex-c"}
        intent = target.get("data")
        if not isinstance(intent, dict) or not intent.get("provider") or not intent.get("model"):
            raise RuntimeError("historical startup target requires separate review")
        normalized_model = intent["model"]
        if intent["provider"] in codex and normalized_model == "gpt-6-astra":
            normalized_model = "gpt-6.1-sol"
        applied = False
        for entry in branch:
            data = entry.get("data")
            if (
                entry.get("type") == "custom"
                and entry.get("customType") == "cross-family-model-target-applied"
                and isinstance(data, dict)
                and data.get("targetEntryId") == target["id"]
                and data.get("provider") == intent["provider"]
                and data.get("model") == normalized_model
                and (data.get("provider") not in codex or data.get("model") == "gpt-6.1-sol")
            ):
                applied = True
        if not applied:
            raise RuntimeError("historical startup target requires separate review")


def audit():
    records = []
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if (
                process / "comm"
            ).read_text().strip() != "pi" or process.stat().st_uid != os.getuid():
                continue
            values = (process / "environ").read_text().split("\0")
            install = next(
                (
                    value.split("=", 1)[1]
                    for value in values
                    if value.startswith("PI_FLEET_INSTALL=")
                ),
                None,
            )
            source = (
                Path(install) / "node_modules/@earendil-works/pi-ai/dist/api/anthropic-messages.js"
                if install
                else None
            )
            body = source.read_text() if source else ""
            known = bool(body)
            affected = all(
                marker in body
                for marker in [
                    "gatewayPreEgressTrust",
                    "deadlineTimer",
                    "Request deadline exceeded",
                ]
            )
            records.append(
                {"pid": int(process.name), "install": install, "affected": affected, "known": known}
            )
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError):
            records.append(
                {"pid": int(process.name), "install": None, "affected": None, "known": False}
            )
    result = {
        "nativeProcesses": len(records),
        "affected": sum(record["affected"] is True for record in records),
        "unknown": sum(not record["known"] for record in records),
        "records": records,
    }
    print(json.dumps(result))
    return 1 if not records or result["affected"] or result["unknown"] else 0


def main():
    if sys.argv[1:] == ["--audit"]:
        raise SystemExit(audit())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "qualify-bootstrap", "restart"])
    parser.add_argument("--session", required=True)
    parser.add_argument("--pane", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--accepted-install", required=True)
    parser.add_argument("--accepted-source", required=True)
    parser.add_argument("--nonce")
    parser.add_argument(
        "--restore-role",
        help="Explicitly reconstructed original role; never changes selected model",
    )
    parser.add_argument(
        "--repair-bootstrap-identity",
        action="store_true",
        help="Reviewed identity reconstruction through native preregistration",
    )
    args = parser.parse_args()
    # ponytail: identity reconstruction is qualified only for this known incident seat.
    if args.repair_bootstrap_identity and (args.session, args.pane, args.name) != (
        "home",
        "w46:p1E",
        "gama-review",
    ):
        raise RuntimeError("identity reconstruction is qualified only for the original Gama seat")
    if args.provider != "claude-lane":
        raise RuntimeError(
            "this reviewed recovery scope currently supports Claude subscription seats only"
        )
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = Path(args.accepted_install).resolve(strict=True)
    if target != (Path.home() / ".local/share/pi-fleet/current").resolve(strict=True):
        raise RuntimeError("qualified accepted install is no longer current")
    launcher = str(LAUNCHER.resolve(strict=True))
    if not launcher.startswith("/nix/store/"):
        raise RuntimeError("launcher is not immutable")
    if args.phase == "prepare":
        source = Path(args.accepted_source).resolve(strict=True)
        if not str(source).startswith("/nix/store/"):
            raise RuntimeError("accepted source must be immutable")
        accepted_pin = json.loads(
            (source / "packaging/fleet/fleet-pin.x86_64-linux.json").read_text()
        )
        installed_pin = json.loads((target / ".fleet-pin.json").read_text())
        if installed_pin != accepted_pin:
            raise RuntimeError("installed manifest does not match accepted immutable source pin")
        call(
            "bash",
            str(source / "packaging/fleet/fleet-install.sh"),
            "verify",
            "--source",
            str(target),
        )
        require_safe_branch(args.transcript)
        state = agent(args.session, args.pane)
        if state.get("name") != args.name:
            raise RuntimeError("native agent name changed")
        if (
            state.get("agent_status") not in {"idle", "done"}
            or state.get("interactive_ready") is False
        ):
            raise RuntimeError("pane is not settled")
        if (
            state.get("agent_session", {}).get("value") != args.transcript
            or state.get("cwd") != args.cwd
        ):
            raise RuntimeError("pane session/cwd changed")
        process_info = info(args.session, args.pane)
        shell_name = Path(f"/proc/{process_info['shell_pid']}/comm").read_text().strip()
        if args.repair_bootstrap_identity and shell_name not in {"bash", "zsh"}:
            raise RuntimeError("one-shot native registration requires the original Bash/Zsh shell")
        processes = process_info["foreground_processes"]
        if len(processes) != 1 or processes[0]["name"] != "pi":
            raise RuntimeError("ambiguous native process")
        pid = processes[0]["pid"]
        env = dict(
            item.split("=", 1)
            for item in Path(f"/proc/{pid}/environ").read_text().split("\0")
            if "=" in item
        )
        role = env.get("PI_ROLE_RESOLVED")
        target_role = args.restore_role or (role.rstrip("~") if role else None)
        if not target_role or not all(
            character.isalnum() or character in "_-" for character in target_role
        ):
            raise RuntimeError("original bootstrap role is not qualified")
        nonce = secrets.token_hex(32)
        request = {
            "schema": 1,
            "nonce": nonce,
            "expires": int(time.time() * 1000) + 240000,
            "pid": pid,
            "session": args.transcript,
            "cwd": args.cwd,
            "provider": args.provider,
            "model": args.model,
            "effort": args.effort,
            "install": env["PI_FLEET_INSTALL"],
            "acceptedInstall": str(target),
            "launcher": launcher,
            "launcherSha256": fingerprint(launcher),
            "herdrSession": args.session,
            "pane": args.pane,
            "name": args.name,
            "shellPid": process_info["shell_pid"],
        }
        request["shellName"] = shell_name
        request["role"] = role
        request["identityRepair"] = args.repair_bootstrap_identity
        request["targetRole"] = target_role
        bootstrap_keys = [
            "PI_WORKER",
            "PI_GENERATOR_FAMILY",
            "PI_ROLE_RESOLVED",
            "PI_ROLE_SOURCE",
            "PI_ROLE_MODEL_TIER",
            "PI_ROLE_SKILL_COUNT",
            "HERDR_AGENT_NAME",
            "PLANE_MAPPING_REQUIRED",
            "PLANE_PROJECT",
            "PLANE_WRITER",
            "PLANE_KEY_ENTRY",
            "PLANE_WORKSPACE",
            "PI_PROJECT_RESOLVED",
            "PI_FLEET_NODE_ARGS",
            "PI_IMAGE_PROTOCOL",
            "HERDR_PROMPT_BIN_PATH",
            "HERDR_BIN_PATH",
            "PI_HERDR_PANE_OWNED",
            "PI_ARTIFACT_ROOT",
            "PI_CACHE_RETENTION",
            "PI_CODING_AGENT_DIR",
            "UV_THREADPOOL_SIZE",
            "RAYON_NUM_THREADS",
            "TOKIO_WORKER_THREADS",
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "MALLOC_ARENA_MAX",
            "CAVEMAN_DEFAULT_MODE",
            "CAVEMAN_ACTIVATE_HOOK",
        ]
        request["bootstrapEnv"] = {key: env[key] for key in bootstrap_keys if key in env}
        # Pin the native supported root without modifying the live current symlink.
        launch_root = ROOT / f"{nonce}.launch-root"
        launch_root.mkdir(mode=0o700)
        (launch_root / "current").symlink_to(target)
        for name in ["fleet-wrapper.sh", "homegrown-core.sh"]:
            source = (Path.home() / ".local/share/pi-fleet" / name).resolve(strict=True)
            if not str(source).startswith("/nix/store/"):
                raise RuntimeError("managed launch input is not immutable")
            (launch_root / name).symlink_to(source)
        request["launchRoot"] = str(launch_root)
        with open(
            ROOT / f"{nonce}.request.json", "x", opener=lambda p, f: os.open(p, f, 0o600)
        ) as output:
            json.dump(request, output)
        print(
            json.dumps({"nonce": nonce, "request": str(ROOT / f"{nonce}.request.json"), "pid": pid})
        )
        return
    if (
        not args.nonce
        or len(args.nonce) != 64
        or any(c not in "0123456789abcdef" for c in args.nonce)
    ):
        raise RuntimeError("invalid nonce")
    if args.phase == "qualify-bootstrap":
        request_path = ROOT / f"{args.nonce}.request.json"
        request = json.loads(request_path.read_text())
        state = json.loads((ROOT / f"{args.nonce}.state.json").read_text())
        if (
            any(state.get(key) != value for key, value in request.items())
            or request["expires"] <= time.time() * 1000
        ):
            raise RuntimeError("bootstrap state is stale or does not match exact request")
        request["expectedNativeContext"] = require_context_agreement(
            request["session"], state.get("nativeContext")
        )
        native = state["nativeBootstrap"]
        if native.get("qualified") is not True or not native["node"].startswith("/nix/store/"):
            raise RuntimeError("native launch flags or node binary are unrecognized")
        if (
            request["bootstrapEnv"].get("HERDR_AGENT_NAME") != request["name"]
            and request.get("identityRepair") is not True
        ):
            raise RuntimeError(
                "original exact agent bootstrap name is missing; hold for explicit recovery"
            )
        shell_name = Path(f"/proc/{request['shellPid']}/comm").read_text().strip()
        if request.get("identityRepair") is True and shell_name not in {"bash", "zsh"}:
            raise RuntimeError("one-shot native registration requires Bash/Zsh")
        request["shellName"] = shell_name
        request["expectedBootstrapHash"] = native["hash"]
        request["nativeArgs"] = native["replayArgs"]
        request["nativeNode"] = native["node"]
        staging = request_path.with_suffix(".qualified")
        with open(staging, "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            json.dump(request, output)
        staging.replace(request_path)
        print(
            json.dumps(
                {
                    "bootstrapQualified": True,
                    "nonce": args.nonce,
                    "argumentCount": len(native["args"]),
                }
            )
        )
        return
    receipt = json.loads((ROOT / f"{args.nonce}.ready.json").read_text())
    request = json.loads((ROOT / f"{args.nonce}.request.json").read_text())
    if any(receipt.get(key) != value for key, value in request.items()):
        raise RuntimeError("receipt mismatches request")
    if (
        receipt.get("idle") is not True
        or receipt.get("pending") is not False
        or receipt.get("editorBytes") != 0
    ):
        raise RuntimeError("native admission not attested")
    if (
        request["expires"] <= time.time() * 1000
        or request["launcher"] != launcher
        or request["launcherSha256"] != fingerprint(launcher)
    ):
        raise RuntimeError("expired or changed launcher")
    exact = {
        "herdrSession": args.session,
        "pane": args.pane,
        "session": args.transcript,
        "cwd": args.cwd,
        "provider": args.provider,
        "model": args.model,
        "effort": args.effort,
        "acceptedInstall": str(target),
        "name": args.name,
    }
    if any(request.get(key) != value for key, value in exact.items()):
        raise RuntimeError("restart arguments changed")
    if args.restore_role is not None and args.restore_role != request["targetRole"]:
        raise RuntimeError("restored bootstrap role changed")
    require_safe_branch(args.transcript)
    if (Path(request["launchRoot"]) / "current").resolve(strict=True) != target:
        raise RuntimeError("pinned launch root changed")
    deadline = time.monotonic() + 20
    while True:
        if not Path(f"/proc/{request['pid']}").exists() and shell_ready(
            info(args.session, args.pane), request["pid"], request["shellPid"], args.cwd
        ):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("old process did not gracefully settle to original shell")
        time.sleep(0.2)
    # Exclusive durable marker makes a retry incapable of starting a second process.
    marker = ROOT / f"{args.nonce}.started.json"
    with open(marker, "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
        json.dump({"startingAt": time.time(), "request": args.nonce}, output)
    # Execute the reviewed immutable launcher, rather than Herdr's PATH-resolved executable.
    bootstrap = [f"{key}={value}" for key, value in request["bootstrapEnv"].items()]
    # Replay qualified non-selection args without duplicating the top wrapper's generated args.
    command = shlex.join(
        [
            launcher_tool(launcher, "env"),
            f"PI_FLEET_ROOT={request['launchRoot']}",
            f"PI_FLEET_NODE={request['nativeNode']}",
            *bootstrap,
            str(Path(request["launchRoot"]) / "fleet-wrapper.sh"),
            *request["nativeArgs"],
            "--session",
            args.transcript,
            "--model",
            f"{args.provider}/{args.model}",
            "--thinking",
            args.effort,
        ]
    )
    if request.get("identityRepair") is True:
        start_registered_repair(args, request, launcher)
        detect_native_start(args)
    else:
        dispatch_and_detect(args, command)
    # pane.run starts the exact binary but does not register a native agent name.
    call("herdr", "--session", args.session, "agent", "rename", args.pane, args.name)
    state = agent(args.session, args.pane)
    if (
        state.get("name") != args.name
        or state.get("agent_session", {}).get("value") != args.transcript
        or state.get("cwd") != args.cwd
    ):
        raise RuntimeError("new native identity did not preserve session/cwd")
    processes = info(args.session, args.pane)["foreground_processes"]
    if len(processes) != 1 or processes[0]["name"] != "pi":
        raise RuntimeError("new process identity unclear")
    new_pid = processes[0]["pid"]
    env = dict(
        item.split("=", 1)
        for item in Path(f"/proc/{new_pid}/environ").read_text().split("\0")
        if "=" in item
    )
    if Path(env["PI_FLEET_INSTALL"]).resolve() != target:
        raise RuntimeError("new process does not use accepted artifact")
    print(
        json.dumps(
            {
                "oldPid": request["pid"],
                "newPid": new_pid,
                "install": str(target),
                "sessionPreserved": True,
                "requestedProvider": args.provider,
                "requestedModel": args.model,
                "requestedEffort": args.effort,
                "nativeSelectionVerification": "requires core-upgrade-state receipt",
                "promptSent": False,
            }
        )
    )


if __name__ == "__main__":
    main()
