#!/usr/bin/env python3
"""Read-only proof that the reviewed cohort retained its original work context."""

import argparse
import hashlib
import json
from pathlib import Path


def lineage(entries, leaf=None):
    indexed = {
        entry["id"]: entry
        for entry in entries
        if entry.get("id") and entry.get("type") != "session"
    }
    if len(indexed) != len(entries) - 1 or entries[0].get("type") != "session":
        raise ValueError("ambiguous transcript entries")
    current = entries[-1]["id"] if leaf is None else leaf
    result, seen = [], set()
    while current is not None:
        if current in seen or current not in indexed:
            raise ValueError("ambiguous transcript lineage")
        seen.add(current)
        entry = indexed[current]
        result.append(entry)
        current = entry["parentId"]
    return result


def check(baseline, identity, targets):
    if baseline["transcript"] != identity["native"]["session"]:
        raise ValueError("transcript identity changed")
    data = Path(baseline["transcript"]).read_bytes()
    lines = data.splitlines(keepends=True)
    original = b"".join(lines[: baseline["rawEntries"]])
    if hashlib.sha256(original).hexdigest() != baseline["sha256"]:
        raise ValueError("original transcript prefix changed")
    before = [json.loads(line) for line in original.splitlines() if line]
    after = [json.loads(line) for line in lines if line.strip()]
    branch = lineage(after)
    restored = identity["contextDetached"]
    target = targets.get((identity["herdrSession"], identity["pane"]))
    old_branch = lineage(before, target) if restored else lineage(before)
    old_work = {entry["id"] for entry in old_branch if entry["type"] == "message"}
    new_work = {entry["id"] for entry in branch if entry["type"] == "message"}
    if not old_work or not old_work.issubset(new_work):
        raise ValueError("original active work context is absent")
    if restored and not any(
        entry.get("customType") == "pi-context-branch-recovery"
        and entry.get("parentId") == target
        for entry in branch
    ):
        raise ValueError("reviewed native restoration audit is absent")
    return {
        "pane": identity["pane"],
        "session": identity["herdrSession"],
        "prefixUnchanged": True,
        "originalWorkMessages": len(old_work),
        "currentWorkMessages": len(new_work),
        "restored": restored,
        "passed": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--pane")
    args = parser.parse_args()
    baseline = {
        (row["session"], row["pane"]): row
        for row in json.loads(args.baseline.read_text())["rows"]
    }
    targets = {
        (row["session"], row["pane"]): row["targetLeaf"]
        for row in json.loads(args.targets.read_text())
    }
    identities = json.loads(args.manifest.read_text())
    rows = [row for row in identities if args.pane is None or row["pane"] == args.pane]
    if not rows or len({(row["herdrSession"], row["pane"]) for row in rows}) != len(rows):
        raise ValueError("missing or ambiguous cohort")
    results = []
    for row in rows:
        try:
            results.append(check(baseline[(row["herdrSession"], row["pane"])], row, targets))
        except (OSError, ValueError, KeyError, TypeError) as error:
            results.append({"pane": row["pane"], "passed": False, "error": type(error).__name__})
    print(json.dumps({"readOnly": True, "results": results}))
    raise SystemExit(0 if all(row["passed"] for row in results) else 1)


if __name__ == "__main__":
    main()
