#!/usr/bin/env python3
"""Apply one explicit human Fable cohort order using native commands, never inference."""

import argparse
import asyncio
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

SOURCE = ("claude-lane", "claude-fable-5-2")
TARGET = ("claude-lane", "claude-opus-5-5")


async def execute(kind, args):
    if kind == "herdr":
        process = await asyncio.create_subprocess_exec(
            "/etc/profiles/per-user/notroot/bin/herdr",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    elif kind == "prompt":
        process = await asyncio.create_subprocess_exec(
            "/etc/profiles/per-user/notroot/bin/herdr-prompt",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        raise ValueError("unsupported executable")
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        # Do not echo command output: failed delivery may contain a human draft.
        raise subprocess.CalledProcessError(process.returncode, kind)
    return stdout.decode()


def call(kind, *args):
    return asyncio.run(execute(kind, args))


def active_lineage(path, allow_pruned=False):
    """Follow parent links; abandoned branch selections cannot authorize a change."""
    entries = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    identified = [entry for entry in entries if entry.get("id")]
    index = {entry["id"]: entry for entry in identified}
    if not identified or len(index) != len(identified):
        raise ValueError("unknown native branch")
    current = identified[-1]["id"]
    seen = set()
    chain = []
    while current is not None:
        if current in seen:
            raise ValueError("cyclic native branch")
        if current not in index:
            if allow_pruned:
                break
            raise ValueError("ambiguous native branch")
        seen.add(current)
        chain.append(index[current])
        current = index[current].get("parentId")
    return chain


def active_selection(path):
    model = effort = None
    for entry in active_lineage(path):
        if model is None and entry.get("type") == "model_change":
            model = (entry.get("provider"), entry.get("modelId"))
        if effort is None and entry.get("type") == "thinking_level_change":
            effort = entry.get("thinkingLevel")
    return model, effort


def visible_state(text):
    """Require identifiable Pi editor borders and a current footer, not transcript prose."""
    lines = text.splitlines()
    rules = [
        i
        for i, line in enumerate(lines)
        if len(line.strip()) >= 20
        and (
            re.fullmatch(r"[─━-]+", line.strip())
            or re.fullmatch(
                r"── [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] Retrying \(\d+/\d+\) in \d+s\.\.\. \(escape to cancel\) ─+",
                line.strip(),
            )
        )
    ]
    if len(rules) < 2:
        raise ValueError("unknown visible editor")
    start, end = rules[-2:]
    footer = "\n".join(lines[end + 1 :])
    if "🧠" not in footer:
        raise ValueError("unknown native footer")
    if any(line.strip() for line in lines[start + 1 : end]):
        raise ValueError("human editor draft present")
    return footer


def footer_selection(footer):
    # Goal/status prose beneath the footer is not the selected-model oracle.
    rows = footer.splitlines()
    model_rows = [line for line in rows if "🤖" in line]
    effort_rows = [line for line in rows if "⚡" in line]
    model_text = "\n".join(model_rows) if model_rows else footer
    effort_text = "\n".join(effort_rows) if effort_rows else footer
    models = [
        model
        for model in (SOURCE, TARGET)
        if model[0] in model_text
        and any(
            re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", model_text)
            for token in (model[1], model[1].removeprefix("claude-"))
        )
    ]
    efforts = re.findall(r"(?<![\w-])(off|minimal|low|medium|high|xhigh|max)(?![\w-])", effort_text)
    if len(models) != 1 or len(set(efforts)) != 1:
        raise ValueError("native visible selection ambiguous")
    return models[0], efforts[0]


def inspect(seat):
    state = json.loads(
        call("herdr", "--session", seat["session"], "agent", "get", seat["pane_id"])
    )["result"]["agent"]
    identity = state.get("agent_session", {})
    if (
        state.get("agent") != "pi"
        or state.get("cwd") != seat["cwd"]
        or identity.get("source") != "herdr:pi"
        or identity.get("value") != seat["transcript"]
    ):
        raise ValueError("native pane/session/cwd identity changed")
    visible = call(
        "herdr",
        "--session",
        seat["session"],
        "agent",
        "read",
        seat["pane_id"],
        "--source",
        "visible",
        "--lines",
        "40",
        "--format",
        "text",
    )
    footer = visible_state(visible)
    try:
        model, effort = active_selection(seat["transcript"])
    except ValueError as error:
        if str(error) != "ambiguous native branch":
            raise
        # A pruned historical parent cannot defeat fresh exact native footer evidence.
        return footer_selection(footer)
    # A missing native model-change event requires a visible selected-model oracle.
    if model is None:
        model, visible_effort = footer_selection(footer)
        effort = effort or visible_effort
    elif effort is None:
        # Native model is proved; a truncated model footer must not erase its identity.
        rows = [line for line in footer.splitlines() if "⚡" in line]
        values = re.findall(
            r"(?<![\w-])(off|minimal|low|medium|high|xhigh|max)(?![\w-])", "\n".join(rows)
        )
        if len(values) != 1:
            raise ValueError("native effort footer ambiguous")
        effort = values[0]
    return model, effort


def append_receipt(path, record):
    data = (json.dumps({"at": time.time(), **record}, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def migrate(seat, root, apply):
    key = f"{seat['session']}:{seat['pane_id']}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    receipt = root / f"{digest}.jsonl"
    with open(root / f"{digest}.lock", "a", opener=lambda p, f: os.open(p, f, 0o600)) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base = {"seat": key, "transcript": seat["transcript"]}
        try:
            model, effort = inspect(seat)
            if model == TARGET and effort == "max":
                append_receipt(receipt, {**base, "status": "verified"})
                return {"seat": key, "status": "verified"}
            attempts = (
                [
                    json.loads(line)
                    for line in receipt.read_text().splitlines()
                    if json.loads(line).get("status") == "attempt"
                ]
                if receipt.exists()
                else []
            )
            if model == TARGET and effort == "high":
                # Complete only the unattempted effort phase after a proved model transition.
                if any(row.get("command") == "/thinking max" for row in attempts):
                    raise ValueError("previous thinking attempt unresolved; no resend")
                commands = ("/thinking max",)
            else:
                if attempts:
                    raise ValueError("previous attempt unresolved; inspect before any resend")
                if model != SOURCE:
                    raise ValueError("selected model outside human Fable cohort")
                commands = (f"/model {TARGET[0]}/{TARGET[1]}", "/thinking max")
            if not apply:
                return {"seat": key, "status": "qualified"}
            for command in commands:
                # Re-read native identity, editor and branch immediately before each command.
                current, current_effort = inspect(seat)
                if current not in (SOURCE, TARGET):
                    raise ValueError("selection changed during migration")
                if command.startswith("/model") and current != SOURCE:
                    raise ValueError("selection changed before model command")
                if command.startswith("/thinking"):
                    if current != TARGET:
                        raise ValueError("target model not selected before thinking command")
                    if current_effort == "max":
                        continue
                append_receipt(receipt, {**base, "status": "attempt", "command": command})
                try:
                    call("prompt", "-s", seat["session"], seat["pane_id"], command)
                except (TimeoutError, subprocess.CalledProcessError):
                    # Delivery may have succeeded. Inspect once; never blindly resend.
                    after = inspect(seat)
                    expected = after[0] == TARGET and (
                        not command.startswith("/thinking") or after[1] == "max"
                    )
                    if not expected:
                        raise ValueError("delivery unverified; no resend") from None
            if inspect(seat) != (TARGET, "max"):
                raise ValueError("native target selection not verified")
            append_receipt(receipt, {**base, "status": "verified"})
            return {"seat": key, "status": "verified"}
        except (ValueError, OSError, TimeoutError, subprocess.CalledProcessError) as error:
            # Fixed exception messages only; no visible pane text or subprocess output.
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            append_receipt(receipt, {**base, "status": "held", "reason": reason})
            return {"seat": key, "status": "held", "reason": reason}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    if (
        snapshot.get("humanOrder")
        != "Our fable limit is over again, migrate all the fable panes to use opus max"
        or snapshot.get("humanOrderTime") != "11/11/2026 12:36"
    ):
        parser.error("snapshot lacks this exact human order")
    seats = snapshot["seats"]
    keys = [(seat["session"], seat["pane_id"]) for seat in seats]
    if len(set(keys)) != len(keys):
        parser.error("duplicate cohort panes")
    if any((seat["selection"]["provider"], seat["selection"]["model"]) != SOURCE for seat in seats):
        parser.error("snapshot includes a model outside the human cohort")
    args.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda seat: migrate(seat, args.receipts, args.apply), seats))
    print(json.dumps(results))
    return int(any(result["status"] == "held" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
