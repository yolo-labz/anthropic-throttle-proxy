"""Content-free native Pi history census; observed arithmetic, never realized savings.

Keep the resulting directory private. It contains hashes, timestamps and usage, not
prompts. Read each file to its initial byte boundary; report mutation and every gap.
No source, model, service, provider or acquisition cursor is changed.
"""

import argparse
import hashlib
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

MEASURES = ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens")
COMPONENTS = ("input", "output", "cacheRead", "cacheWrite")
MAX_LINE = 32 << 20


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value >= 0:
            return value
    return None


def epoch(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def discover(home):
    roots = [(f"pi:{p.name}", p / "sessions") for p in sorted(home.glob(".pi/agent*"))]
    roots.append(("pi:recovery-archives", home / ".pi/agent/recovery-archives"))
    return [(name, p) for name, root in roots for p in sorted(root.rglob("*.jsonl"))]


def event_row(entry, session, model, provider, cutoff):
    kind = entry.get("type")
    if kind == "message":
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        kind = "assistant"
        usage = message.get("usage")
        model, provider = message.get("model"), message.get("provider")
        body = message.get("content")
        status = message.get("stopReason")
    elif kind == "compaction":
        usage, body, status = entry.get("usage"), entry.get("summary"), None
    else:
        return None
    stamp = epoch(entry.get("timestamp"))
    if (
        not isinstance(entry.get("id"), str)
        or not entry["id"]
        or not isinstance(session, str)
        or not session
        or stamp is None
    ):
        raise ValueError("unidentifiable usage event")
    if stamp >= cutoff:
        return None
    usage = usage if isinstance(usage, dict) else {}
    row = {k: number(usage.get(k)) for k in MEASURES}
    row.update(
        event=digest(json.dumps([kind, entry["id"], stamp])),
        session=digest(session),
        epoch=stamp,
        kind=kind,
        model=model if isinstance(model, str) else None,
        provider=provider if isinstance(provider, str) else None,
        attribution="assistant_record" if kind == "assistant" else "selected_not_wire_attested",
        status=status if isinstance(status, str) else None,
    )
    # Reasoning is a component of native output, not an extra debit.
    row["component_total"] = (
        sum(row[k] for k in COMPONENTS) if all(row[k] is not None for k in COMPONENTS) else None
    )
    cost = usage.get("cost")
    row["list_cost"] = number(cost.get("total")) if isinstance(cost, dict) else None
    body_hash = digest(json.dumps(body, sort_keys=True, ensure_ascii=True))
    identity = {k: v for k, v in row.items() if k != "session"}
    row["fingerprint"] = digest(json.dumps([identity, body_hash], sort_keys=True))
    return row


def insert(db, row):
    payload = json.dumps(row, sort_keys=True, allow_nan=False)
    old = db.execute("SELECT fingerprint FROM events WHERE id=?", (row["event"],)).fetchone()
    if old is None:
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?, 0, 1)", (row["event"], row["fingerprint"], payload)
        )
        return "unique"
    conflict = old[0] != row["fingerprint"]
    db.execute(
        "UPDATE events SET copies=copies+1, conflicted=MAX(conflicted,?) WHERE id=?",
        (int(conflict), row["event"]),
    )
    return "conflicting_copies" if conflict else "duplicate_copies"


def scan_file(db, name, path, cutoff):
    counts = defaultdict(int)
    record = {"source": name, "path_hash": digest(str(path)), "counts": counts}
    model = provider = session = None
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            boundary, read_bytes = before.st_size, 0
            sha = hashlib.sha256()
            oversized = False
            while read_bytes < boundary:
                raw = stream.readline(min(MAX_LINE, boundary - read_bytes))
                if not raw:
                    counts["truncated_during_read"] += 1
                    break
                read_bytes += len(raw)
                sha.update(raw)
                if not raw.endswith(b"\n"):
                    oversized = True
                    if read_bytes == boundary:
                        counts["incomplete_tail"] += 1
                    continue
                if oversized:
                    counts["oversized_lines"] += 1
                    oversized = False
                    continue
                try:
                    entry = json.loads(raw)
                    if not isinstance(entry, dict):
                        raise ValueError("non-object entry")
                    kind = entry.get("type")
                    if kind == "session":
                        session = entry.get("id")
                    elif kind == "model_change":
                        model, provider = entry.get("modelId"), entry.get("provider")
                    row = event_row(entry, session, model, provider, cutoff)
                    if row:
                        counts[insert(db, row)] += 1
                except (ValueError, TypeError, OverflowError):
                    counts["invalid_or_unidentifiable_lines"] += 1
            after = os.fstat(stream.fileno())
            current = path.stat()
            record.update(
                initial_bytes=boundary,
                read_bytes=read_bytes,
                prefix_sha256=sha.hexdigest(),
                changed_during_read=(before.st_size, before.st_mtime_ns, before.st_ino)
                != (after.st_size, after.st_mtime_ns, current.st_ino),
            )
    except OSError as exc:
        record["read_error"] = type(exc).__name__
    return record


def aggregate(db, cap):
    groups = {}
    for (payload,) in db.execute("SELECT payload FROM events WHERE conflicted=0"):
        row = json.loads(payload)
        day = datetime.fromtimestamp(row["epoch"], UTC).strftime("%Y-%m-%d")
        key = (day, row["provider"], row["model"])
        if key not in groups:
            groups[key] = {
                "day_utc": day,
                "provider": row["provider"],
                "model": row["model"],
                "kinds": {},
                "gross_output_cap_removed": 0,
            }
        group = groups[key]
        stats = group["kinds"].setdefault(
            row["kind"],
            {
                "events": 0,
                "status": {},
                "sum": defaultdict(float),
                "missing": defaultdict(int),
                "native_total_disagreements": 0,
            },
        )
        stats["events"] += 1
        status = row["status"] or "unknown"
        stats["status"][status] = stats["status"].get(status, 0) + 1
        for field in (*MEASURES, "component_total", "list_cost"):
            if row[field] is None:
                stats["missing"][field] += 1
            else:
                stats["sum"][field] += row[field]
        if row["totalTokens"] is not None and row["component_total"] is not None:
            stats["native_total_disagreements"] += row["totalTokens"] != row["component_total"]
        if row["kind"] == "compaction" and row["output"] is not None:
            group["gross_output_cap_removed"] += max(0, row["output"] - cap)
    return sorted(
        groups.values(), key=lambda g: (g["day_utc"], g["provider"] or "", g["model"] or "")
    )


def opportunities(daily):
    models = {}
    for day in daily:
        key = (day["provider"], day["model"])
        row = models.setdefault(
            key,
            {
                "provider": key[0],
                "model": key[1],
                "days": [],
                "events": 0,
                "compactions": 0,
                "output_observed": 0,
                "component_total_observed": 0,
                "compaction_output_observed": 0,
                "gross_output_cap_removed": 0,
                "missing_output_events": 0,
                "missing_component_total_events": 0,
            },
        )
        row["days"].append(day["day_utc"])
        row["gross_output_cap_removed"] += day["gross_output_cap_removed"]
        for kind, stats in day["kinds"].items():
            row["events"] += stats["events"]
            row["output_observed"] += stats["sum"].get("output", 0)
            row["component_total_observed"] += stats["sum"].get("component_total", 0)
            row["missing_output_events"] += stats["missing"].get("output", 0)
            row["missing_component_total_events"] += stats["missing"].get("component_total", 0)
            if kind == "compaction":
                row["compactions"] += stats["events"]
                row["compaction_output_observed"] += stats["sum"].get("output", 0)
    for row in models.values():
        removed = row["gross_output_cap_removed"]
        row["cap_fraction_of_observed_output"] = (
            removed / row["output_observed"] if row["output_observed"] > 0 else None
        )
        for label, denom, missing in (
            ("cap_fraction_of_all_output", row["output_observed"], row["missing_output_events"]),
            (
                "cap_fraction_of_all_component_tokens",
                row["component_total_observed"],
                row["missing_component_total_events"],
            ),
        ):
            row[label] = removed / denom if denom > 0 and not missing else None
    return sorted(models.values(), key=lambda r: -r["gross_output_cap_removed"])


def run(sources, output, cutoff, cap):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    db_path = output / "events.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA cache_size=-16000")
    db.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, fingerprint TEXT, payload TEXT, "
        "conflicted INTEGER, copies INTEGER)"
    )
    totals, seen = defaultdict(int), set()
    with (output / "sources.jsonl").open("x") as receipts:
        for name, path in sources:
            try:
                stat = path.stat()
                identity = stat.st_dev, stat.st_ino
                if identity in seen:
                    totals["duplicate_file_aliases"] += 1
                    continue
                seen.add(identity)
            except OSError:
                totals["stat_probe_errors"] += 1
                # scan_file also records the concrete read error in the source receipt.
            receipt = scan_file(db, name, path, cutoff)
            receipts.write(json.dumps(receipt, sort_keys=True) + "\n")
            totals["files"] += 1
            totals["bytes_read"] += receipt.get("read_bytes", 0)
            totals["read_errors"] += int("read_error" in receipt)
            totals["changed_files"] += int(receipt.get("changed_during_read", False))
            for key, value in receipt["counts"].items():
                totals[key] += value
            if totals["files"] % 500 == 0:
                print(json.dumps({"files_scanned": totals["files"]}), flush=True)
    db.commit()
    daily = aggregate(db, cap)
    total, conflicts = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(conflicted),0) FROM events"
    ).fetchone()
    summary = {
        "cutoff_epoch_exclusive": cutoff,
        "cap": cap,
        "capture": dict(totals),
        "unique_events": total,
        "conflicted_events_excluded": conflicts,
        "opportunities": opportunities(daily),
        "realized_savings": None,
        "accepted_task_outcomes": None,
        "billing_or_quota_savings": None,
        "scope": "retained native Pi files only; not a census of all provider requests",
        "attribution": "assistant record; compaction latest selected model, not wire-attested",
        "inference": "descriptive census; no causal, future-performance or quality claim",
    }
    db.close()
    for name, value in (("daily.json", daily), ("summary.json", summary)):
        (output / name).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    for path in output.iterdir():
        path.chmod(0o600)
    manifest = {}
    for path in output.iterdir():
        with path.open("rb") as stream:
            manifest[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest["analysis_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (output / "sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "sha256.json").chmod(0o600)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cutoff", required=True, help="exclusive ISO timestamp with timezone")
    parser.add_argument(
        "--cap", type=int, default=16384, help="arithmetic output-token bound, not a policy"
    )
    args = parser.parse_args()
    cutoff = epoch(args.cutoff)
    if cutoff is None or args.cap < 0:
        parser.error("cutoff needs a timezone; cap must be nonnegative")
    summary = run(discover(args.home), args.output, cutoff, args.cap)
    print(json.dumps({k: v for k, v in summary.items() if k != "opportunities"}))


if __name__ == "__main__":
    main()
