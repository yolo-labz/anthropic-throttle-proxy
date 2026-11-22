#!/usr/bin/env python3
"""Read-only actual native Pi core census; metadata only, never inference."""

import argparse
import concurrent.futures
import importlib.util
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

import recover_core as core


def collect(model_helper):
    agents = {}
    for session in ("home", "infra", "products", "side-projects", "study"):
        data = json.loads(core.call("herdr", "--session", session, "agent", "list"))
        for agent in data["result"]["agents"]:
            if agent.get("agent") == "pi":
                agents[(session, agent["pane_id"])] = agent
    processes = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid() or (path / "comm").read_text().strip() != "pi":
                continue
            env = dict(
                item.split("=", 1)
                for item in (path / "environ").read_text().split("\0")
                if "=" in item
            )
            processes.append((int(path.name), env))
        except (OSError, ValueError):
            continue

    def qualify(process):
        pid, env = process
        session, pane = env.get("HERDR_SESSION"), env.get("HERDR_PANE_ID")
        row = {
            "pid": pid,
            "session": session,
            "pane": pane,
            "install": env.get("PI_FLEET_INSTALL"),
            "role": env.get("PI_ROLE_RESOLVED"),
            "bootstrapName": env.get("HERDR_AGENT_NAME"),
            "holds": [],
        }
        try:
            source = Path(row["install"]) / (
                "node_modules/@earendil-works/pi-ai/dist/api/anthropic-messages.js"
            )
            body = source.read_text()
            row["affected"] = all(
                marker in body
                for marker in (
                    "gatewayPreEgressTrust",
                    "deadlineTimer",
                    "Request deadline exceeded",
                )
            )
            agent = agents.get((session, pane))
            if agent is None:
                row["holds"].append("native_agent_missing")
                return row
            identity = agent.get("agent_session", {})
            row.update(
                {
                    "name": agent.get("name"),
                    "status": agent.get("agent_status"),
                    "cwd": agent.get("cwd"),
                    "transcript": identity.get("value"),
                }
            )
            if identity.get("source") != "herdr:pi":
                raise ValueError("native_session_unproven")
            foreground = core.info(session, pane)["foreground_processes"]
            if len(foreground) != 1 or foreground[0].get("pid") != pid:
                raise ValueError("foreground_pid_changed")
            if row["bootstrapName"] != row["name"] or not row["name"]:
                row["holds"].append("bootstrap_name_missing_or_mismatch")
            if not row["role"]:
                row["holds"].append("native_role_unknown")
            seat = {
                "session": session,
                "pane_id": pane,
                "cwd": row["cwd"],
                "transcript": row["transcript"],
            }
            # Selection itself may be checked while working; no editor interaction occurs.
            try:
                model, effort = model_helper.active_selection(row["transcript"])
                if model is None or effort is None:
                    model, effort = model_helper.inspect(seat)
            except ValueError:
                model, effort = model_helper.inspect(seat)
            if model is None:
                raise ValueError("native_model_unknown")
            row.update({"provider": model[0], "model": model[1], "effort": effort})
            try:
                core.require_safe_branch(row["transcript"])
                row["startupBranchSafe"] = True
            except RuntimeError:
                row["startupBranchSafe"] = False
                row["holds"].append("historical_startup_or_ambiguous_branch")
            if effort is None:
                row["holds"].append("native_effort_unknown")
            row["nativeBootstrapStillRequired"] = True
        except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
            row["holds"].append(
                str(error) if isinstance(error, ValueError) else type(error).__name__
            )
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(qualify, processes))
    affected = [row for row in rows if row.get("affected") and row.get("provider") == "claude-lane"]

    def group(row):
        if row["holds"]:
            return "held"
        return (
            "identity_eligible_idle" if row["status"] in {"idle", "done"} else "working_or_unknown"
        )

    return {
        "at": time.time(),
        "readOnly": True,
        "nativeProcesses": len(rows),
        "affectedNativeProcesses": sum(bool(row.get("affected")) for row in rows),
        "affectedClaudeSeats": len(affected),
        "groups": dict(Counter(group(row) for row in affected)),
        "holds": dict(Counter(hold for row in affected for hold in row["holds"])),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-helper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("migration", args.model_helper)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    result = collect(helper)
    with open(args.output, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump(result, output)
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}))


if __name__ == "__main__":
    main()
