#!/usr/bin/env python3
"""One bounded idle-boundary upgrade. No inference, retry, or forced cancellation."""

import argparse
import asyncio
import concurrent.futures
import ctypes
import fcntl
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import recover_core as core

GUARD_VERSION = "native-load-v1"
HERDR_PROMPT = "/etc/profiles/per-user/notroot/bin/herdr-prompt"
RELOADING = "Reloading keybindings, extensions, skills, prompts, themes, and context files..."
RELOADED = "Reloaded keybindings, extensions, skills, prompts, themes, and context files"


def reload_editor_barrier(deliver, wait_output, initial_screen):
    """Observe a real loading frame before accepting a later restored editor frame."""
    if RELOADING in initial_screen or not empty_editor(initial_screen):
        raise RuntimeError("initial native editor is not qualified")
    # The native waiter must run alongside reload; a missed fast frame holds safely.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        loading = pool.submit(wait_output, RELOADING)
        deliver("/reload")
        first = loading.result()
    if RELOADING not in first["text"]:
        raise RuntimeError("native loading frame was not observed")
    restored = wait_output(RELOADED)
    # Herdr visible reads report revision=0. Freshness comes from the ordered
    # actual loading -> restored frame transition, never a fabricated counter.
    if (
        RELOADED not in restored["text"]
        or RELOADING in restored["text"]
        or not empty_editor(restored["text"])
    ):
        raise RuntimeError("native editor restoration not proven; no state command sent")


class Changes:
    """Linux filesystem notifications; timeout is terminal, never a polling interval."""

    def __init__(self, paths, deadline):
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.deadline = deadline
        try:
            for path in set(map(str, paths)):
                if libc.inotify_add_watch(self.fd, os.fsencode(path), 0x00000FCE) < 0:
                    raise OSError(ctypes.get_errno(), "inotify_add_watch")
        except BaseException:
            os.close(self.fd)
            raise

    def wait(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
            raise TimeoutError("idle recovery deadline; inspect receipt before another attempt")
        os.read(self.fd, 65536)

    def close(self):
        os.close(self.fd)


def empty_editor(text):
    lines = text.splitlines()
    borders = [i for i, line in enumerate(lines) if re.fullmatch(r"[─━-]{20,}", line.strip())]
    if len(borders) < 2:
        return False
    start, end = borders[-2:]
    # A spinner border is deliberately not accepted as an idle editor boundary.
    return "🧠" in "\n".join(lines[end + 1 :]) and not any(
        line.strip() for line in lines[start + 1 : end]
    )


def native_admission(request, state):
    return (
        all(state.get(k) == v for k, v in request.items())
        and state.get("idle") is True
        and state.get("pending") is False
        and state.get("editorBytes") == 0
    )


def last_json(output):
    for line in reversed(output.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise RuntimeError("phase returned no structured receipt")


async def execute(arguments, duration):
    if arguments[0] not in {
        sys.executable,
        "/etc/profiles/per-user/notroot/bin/herdr",
        HERDR_PROMPT,
    }:
        raise ValueError("unsupported operational executable")
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=duration)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise subprocess.TimeoutExpired(arguments, duration) from error
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, arguments, stderr=stderr)
    return stdout.decode()


def run(arguments, deadline, cap=40):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("recovery deadline")
    return asyncio.run(execute(arguments, min(cap, remaining)))


def require_reserve(deadline):
    if deadline - time.monotonic() < 600:
        raise RuntimeError("insufficient bounded restart reserve; no shutdown sent")


def acquire_lock(path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def loaded_generation(pid):
    path = core.ROOT / f"{pid}.loaded.json"
    try:
        stat = path.lstat()
        if (
            not path.is_file()
            or path.is_symlink()
            or stat.st_uid != os.getuid()
            or stat.st_mode & 0o777 != 0o600
        ):
            return None
        body = json.loads(path.read_text())
        if (
            isinstance(body, dict)
            and body.get("pid") == pid
            and body.get("guardVersion") == GUARD_VERSION
            and re.fullmatch(r"[a-f0-9]{64}", body.get("generation", ""))
        ):
            return body["generation"]
        return None
    except (OSError, ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in [
        "session",
        "pane",
        "name",
        "transcript",
        "cwd",
        "provider",
        "model",
        "effort",
        "accepted-install",
        "accepted-source",
    ]:
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--timeout", type=int, default=870, choices=range(1, 871))
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    core.ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = acquire_lock(
        core.ROOT
        / (hashlib.sha256(f"{args.session}:{args.pane}".encode()).hexdigest() + ".controller.lock")
    )
    changes = Changes([Path(args.transcript).parent, core.ROOT], deadline)
    identity = []
    for key, value in vars(args).items():
        if key != "timeout":
            identity.extend(["--" + key.replace("_", "-"), str(value)])
    phase_script = str(Path(__file__).with_name("recover_core.py"))

    def phase(name, nonce=None):
        command = [sys.executable, phase_script, name, *identity]
        if nonce:
            command.extend(["--nonce", nonce])
        return last_json(run(command, deadline, cap=300 if name == "restart" else 40))

    def screen():
        return run(
            [
                "/etc/profiles/per-user/notroot/bin/herdr",
                "--session",
                args.session,
                "agent",
                "read",
                args.pane,
                "--source",
                "visible",
                "--lines",
                "300",
                "--format",
                "text",
            ],
            deadline,
        )

    def current_identity():
        state = json.loads(
            run(
                [
                    "/etc/profiles/per-user/notroot/bin/herdr",
                    "--session",
                    args.session,
                    "agent",
                    "get",
                    args.pane,
                ],
                deadline,
            )
        )["result"]["agent"]
        identity = state.get("agent_session", {})
        if (
            state.get("agent") != "pi"
            or state.get("name") != args.name
            or state.get("cwd") != args.cwd
            or identity.get("source") != "herdr:pi"
            or identity.get("value") != args.transcript
        ):
            raise RuntimeError("native pane identity changed; no command sent")
        return state

    def wait_output(text):
        output = run(
            [
                "/etc/profiles/per-user/notroot/bin/herdr",
                "--session",
                args.session,
                "pane",
                "wait-output",
                args.pane,
                "--source",
                "visible",
                "--lines",
                "300",
                "--match",
                text,
                "--timeout",
                "20000",
            ],
            deadline,
            cap=25,
        )
        read = json.loads(output)["result"]["read"]
        if read.get("pane_id") != args.pane or read.get("source") != "visible":
            raise RuntimeError("native reload frame identity changed")
        return read

    def deliver(command):
        if command != "/reload" and not re.fullmatch(
            r"/core-upgrade-(?:state|ready) [a-f0-9]{64}", command
        ):
            raise RuntimeError("unsupported native recovery command")
        current_identity()
        # Existing native delivery helper checks draft/identity. Each command is sent once.
        try:
            run([HERDR_PROMPT, "-s", args.session, args.pane, command], deadline)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # Delivery status is uncertain. Only the caller's fresh exact ACK or
            # matching native nonce receipt may permit progress; never resend.
            print(
                json.dumps(
                    {
                        "event": "native_delivery_uncertain",
                        "exceptionType": type(error).__name__,
                        "returncode": getattr(error, "returncode", None),
                    }
                ),
                file=sys.stderr,
            )
            return False
        return True

    def receipt(nonce, kind):
        path = core.ROOT / f"{nonce}.{kind}.json"
        previous_deadline = changes.deadline
        changes.deadline = min(previous_deadline, time.monotonic() + 20)
        try:
            while not path.exists():
                changes.wait()
        finally:
            changes.deadline = previous_deadline
        request = json.loads((core.ROOT / f"{nonce}.request.json").read_text())
        state = json.loads(path.read_text())
        if not native_admission(request, state):
            raise RuntimeError("native guard did not attest exact idle identity")
        return state

    try:
        state = current_identity()
        if not core.native_started(state, args.transcript, args.cwd):
            idle_budget = deadline - time.monotonic() - 600
            if idle_budget <= 0:
                raise TimeoutError("no idle wait budget before restart reserve")
            run(
                [
                    "/etc/profiles/per-user/notroot/bin/herdr",
                    "--session",
                    args.session,
                    "agent",
                    "wait",
                    args.pane,
                    "--until",
                    "idle",
                    "--until",
                    "done",
                    "--timeout",
                    str(int(idle_budget * 1000)),
                ],
                deadline,
                cap=idle_budget,
            )
        state = current_identity()
        if not core.native_started(state, args.transcript, args.cwd) or not empty_editor(screen()):
            raise RuntimeError("native idle boundary or empty editor not proven")
        prepared = phase("prepare")
        nonce = prepared["nonce"]
        request = json.loads((core.ROOT / f"{nonce}.request.json").read_text())
        baseline = loaded_generation(request["pid"])
        reload_editor_barrier(deliver, wait_output, screen())
        # Native registration, not a rendered toast, proves these commands are loaded.
        previous_deadline = changes.deadline
        changes.deadline = min(previous_deadline, time.monotonic() + 20)
        try:
            while not (generation := loaded_generation(request["pid"])) or generation == baseline:
                changes.wait()
        finally:
            changes.deadline = previous_deadline
        deliver("/core-upgrade-state " + nonce)
        old = receipt(nonce, "state")
        phase("qualify-bootstrap", nonce)
        require_reserve(deadline)
        deliver("/core-upgrade-ready " + nonce)
        receipt(nonce, "ready")
        phase("restart", nonce)
        fresh = phase("prepare")["nonce"]
        deliver("/core-upgrade-state " + fresh)
        new = receipt(fresh, "state")
        if new["pid"] == old["pid"] or new["install"] != str(Path(args.accepted_install).resolve()):
            raise RuntimeError("replacement native runtime proof failed")
        if new.get("role") != old.get("role"):
            raise RuntimeError("replacement bootstrap role changed")
        print(
            json.dumps(
                {
                    "oldNonce": nonce,
                    "newNonce": fresh,
                    "oldPid": old["pid"],
                    "newPid": new["pid"],
                    "verified": True,
                    "promptSent": False,
                }
            )
        )
    finally:
        changes.close()
        os.close(lock)


if __name__ == "__main__":
    main()
