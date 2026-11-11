#!/usr/bin/env python3
"""Existing-work census; delivery requires explicit settled-fleet confirmation."""

import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "migration", Path(__file__).with_name("pi-fleet-model-migrate.py")
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def disposition(path, resume_pending_user=False):
    chain = migration.active_lineage(path, allow_pruned=True)
    messages = [entry for entry in chain if entry.get("type") == "message"]
    if any(not isinstance(entry.get("message"), dict) for entry in messages):
        raise ValueError("unknown native message")
    latest_assistant = next(
        (entry for entry in messages if entry["message"].get("role") == "assistant"), None
    )
    if latest_assistant is None:
        return "hold_no_assistant"
    latest_message = messages[0]
    role = latest_message["message"].get("role")
    if role == "user" and not resume_pending_user:
        return "hold_unconsumed_human"
    message = latest_assistant["message"]
    # Only structural outcomes authorize a continuation; prose is not a completion oracle.
    if message.get("stopReason") in {"error", "aborted"}:
        error = str(message.get("errorMessage", ""))
        if re.search(
            r"(?i)policy|privacy|refus|permission|unauthori|no space|ENOSPC|hardware", error
        ):
            return "hold_policy_hardware_or_storage"
        return "eligible_pending_user" if role == "user" else "eligible_error"
    if message.get("stopReason") == "toolUse" and (
        role in {"assistant", "toolResult"} or (role == "user" and resume_pending_user)
    ):
        return "eligible_pending_user" if role == "user" else "eligible_unfinished_toolchain"
    return "hold_unconsumed_human" if role == "user" else "hold_completed_or_uncertain"


def inspect(seat, resume_pending_user=False, resume_visible_abort=False):
    key = f"{seat['session']}:{seat['pane_id']}"
    try:
        state = json.loads(
            migration.call("herdr", "--session", seat["session"], "agent", "get", seat["pane_id"])
        )["result"]["agent"]
        status = state.get("agent_status")
        if status not in {"idle", "done"}:
            return {"seat": key, "disposition": "leave_active_or_unknown", "status": status}
        model, effort = migration.inspect(seat)
        if (model, effort) != (migration.TARGET, "max"):
            return {"seat": key, "disposition": "hold_selection_not_opus_max"}
        visible = migration.call(
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
        if re.search(r"(?im)^\s*(Steering:|Follow-up:|.*Alt\+Up to edit.*queued)", visible):
            return {"seat": key, "disposition": "hold_pending_queue"}
        if re.search(r"(?i)compacting|retrying \(\d+/\d+\)", visible):
            return {"seat": key, "disposition": "leave_compacting_or_retrying"}
        outcome = disposition(seat["transcript"], resume_pending_user)
        if resume_visible_abort and outcome == "hold_no_assistant":
            standalone_abort = any(
                line.strip() == "Error: This operation was aborted" for line in visible.splitlines()
            )
            forbidden = re.search(
                r"(?i)policy|privacy|refus|permission|unauthori|no space|ENOSPC|hardware", visible
            )
            if standalone_abort and not forbidden:
                outcome = "eligible_visible_abort"
        return {"seat": key, "disposition": outcome}
    except (ValueError, OSError, TimeoutError, KeyError, subprocess.CalledProcessError) as error:
        # Exception classes only: no private transcript, draft, or provider content.
        return {"seat": key, "disposition": "hold_unverified", "reason": type(error).__name__}


def resume_once(seat, receipts, resume_pending_user=False, resume_visible_abort=False):
    key = f"{seat['session']}:{seat['pane_id']}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    with open(
        receipts / f"{digest}.lock", "a", opener=lambda path, flags: os.open(path, flags, 0o600)
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = receipts / f"{digest}.attempt.json"
        if marker.exists():
            return {"seat": key, "disposition": "hold_previous_attempt_no_resend"}
        fresh = (
            inspect(seat, resume_pending_user, True)
            if resume_visible_abort
            else inspect(seat, True)
            if resume_pending_user
            else inspect(seat)
        )
        if not fresh["disposition"].startswith("eligible_"):
            return fresh
        with open(marker, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            json.dump(
                {
                    "seat": key,
                    "transcript": seat["transcript"],
                    "disposition": fresh["disposition"],
                },
                output,
            )
            output.flush()
            os.fsync(output.fileno())
        try:
            migration.call("prompt", "-s", seat["session"], seat["pane_id"], "Continue the work")
        except (OSError, TimeoutError, subprocess.CalledProcessError):
            # Native transition is evidence only; an uncertain delivery never triggers a resend.
            return {
                "seat": key,
                "disposition": "attempt_unverified_no_resend",
                "after": inspect(seat)["disposition"],
            }
        return {"seat": key, "disposition": "submitted_once", "after": inspect(seat)["disposition"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-pending-user", action="store_true")
    parser.add_argument("--resume-visible-abort", action="store_true")
    parser.add_argument("--gateway-ready-confirmed", action="store_true")
    parser.add_argument("--bulk-complete", action="store_true")
    parser.add_argument("--receipts", type=Path)
    args = parser.parse_args()
    if args.resume and not (args.gateway_ready_confirmed and args.bulk_complete and args.receipts):
        parser.error("resume requires explicit gateway-ready/bulk-complete and durable receipts")
    if args.resume:
        args.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot = json.loads(args.snapshot.read_text())
    if snapshot.get("humanOrderTime") != "11/11/2026 12:36" or snapshot.get("humanOrder") != (
        "Our fable limit is over again, migrate all the fable panes to use opus max"
    ):
        parser.error("snapshot lacks the exact authorized human cohort")
    seats = snapshot["seats"]
    if len({(seat["session"], seat["pane_id"]) for seat in seats}) != len(seats):
        parser.error("duplicate cohort panes")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        action = (
            (
                lambda seat: resume_once(
                    seat, args.receipts, args.resume_pending_user, args.resume_visible_abort
                )
            )
            if args.resume
            else (lambda seat: inspect(seat, args.resume_pending_user, args.resume_visible_abort))
        )
        results = list(pool.map(action, seats))
    print(json.dumps({"readOnly": not args.resume, "seats": results}))


if __name__ == "__main__":
    main()
