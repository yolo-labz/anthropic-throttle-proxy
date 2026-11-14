#!/usr/bin/env python3
"""Read-only provisional fence cohort. Native pending/idle still needs fresh STATE."""

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--controller-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.controller_dir.resolve(strict=True)))
    import recover_context as recovery

    spec = importlib.util.spec_from_file_location(
        "selection", Path(__file__).parents[1] / "pi-fleet-model-migrate.py"
    )
    selection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selection)
    candidates = json.loads(args.audit.read_text())["rows"]

    def inspect(row):
        result = {"herdrSession": row["session"], "pane": row["pane"], "hold": None}
        if row.get("classification") != "old_idle_candidate" or row.get("holds"):
            result["hold"] = "prior_history_or_activity_hold"
            return result
        try:
            pid = row["pid"]
            proc = Path("/proc") / str(pid)
            if proc.joinpath("comm").read_text().strip() != "pi":
                raise ValueError("native_process_changed")
            environment = dict(
                item.split("=", 1)
                for item in proc.joinpath("environ").read_text().split("\0")
                if "=" in item
            )
            identity = {
                "pid": pid,
                "session": row["transcript"],
                "transcript": row["transcript"],
                "cwd": row["cwd"],
                "install": row["install"],
                "role": environment.get("PI_ROLE_RESOLVED"),
                "herdrSession": row["session"],
                "pane": row["pane"],
                "name": environment.get("HERDR_AGENT_NAME"),
            }
            if (
                environment.get("PI_FLEET_INSTALL") != row["install"]
                or not identity["name"]
                or not identity["role"]
            ):
                raise ValueError("bootstrap_identity_unknown")
            if (
                environment.get("HERDR_SESSION") != row["session"]
                or environment.get("HERDR_PANE_ID") != row["pane"]
            ):
                raise ValueError("native_pane_changed")
            model, effort = selection.active_selection(row["transcript"])
            if not model or model[0] != "claude-lane" or not effort:
                raise ValueError("current_selection_unknown")
            identity.update(provider=model[0], model=model[1], effort=effort)
            before = Path(row["transcript"]).read_bytes()
            branch = selection.active_lineage(row["transcript"])
            root = branch[-1]
            if root.get("type") == "custom" or not any(
                e.get("type") == "message" and e.get("message", {}).get("role") == "user"
                for e in branch
            ):
                raise ValueError("work_branch_unknown_or_detached")
            operation = recovery.Recovery(identity, time.monotonic() + 120)
            try:
                native = operation.native_identity()
                screen = operation.screen()
            finally:
                operation.changes.close()
            if not recovery.idle.empty_editor(screen) or "compacting context" in screen.lower():
                raise ValueError("editor_or_compaction_hold")
            if selection.footer_selection(selection.visible_state(screen)) != (model, effort):
                raise ValueError("footer_selection_mismatch")
            if Path(row["transcript"]).read_bytes() != before:
                raise ValueError("history_changed_during_observation")
            return {
                **result,
                "name": identity["name"],
                "native": {key: identity[key] for key in recovery.IDENTITY},
                "contextDetached": False,
                "observedStatus": native["agent_status"],
                "emptyEditor": True,
                "transcriptSha256": hashlib.sha256(before).hexdigest(),
                "pendingProof": "REQUIRED_FRESH_NATIVE_STATE_BEFORE_FENCE",
            }
        except Exception as error:
            result["hold"] = str(error) if isinstance(error, ValueError) else type(error).__name__
            return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(inspect, candidates))
    eligible = [row for row in rows if row.get("native")]
    report = {
        "readOnly": True,
        "generatedAt": time.time(),
        "noInputs": True,
        "eligibleProvisional": len(eligible),
        "holds": dict(Counter(row["hold"] for row in rows if row["hold"])),
        "rows": rows,
    }
    with open(args.output, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
        json.dump(report, output, indent=2)
    manifest = args.output.with_suffix(".manifest.json")
    with open(manifest, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
        json.dump(eligible, output, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}))


if __name__ == "__main__":
    main()
