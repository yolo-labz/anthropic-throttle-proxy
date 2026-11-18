#!/usr/bin/env python3
"""Emit ``usage-summary.v1`` (spec 380) from Root's deduplicated usage receipt.

Stdlib only. Inputs:
  * the receipt written by ``dedupe-usage.py`` (read-only): per kind/provider/model
    totals, scan counts, and the top files by cache write with their initial
    herdr pane binding;
  * session METADATA for those top files: only entry type, timestamp and the
    provider/model fields are inspected, to learn which single model a file ran.
    No message, tool argument or tool result is ever emitted.

Honesty rules (the panel renders ``null`` as *unknown*, never zero):
  * every token class stays its own field; nothing is folded together;
  * ``reasoning`` is ``null`` -- the receipt does not carry it;
  * a class with any row the receipt could not count (``unknown_<field>``) is ``null``;
  * classes a provider never reports (``NOT_REPORTED``) are ``null``, not ``0``;
  * a top file is attributed to its pane only when it has exactly one pane binding
    AND every in-window usage row ran one provider/model; otherwise its usage stays
    in the unattributed (``pane: null``) remainder. Attributed rows are subtracted
    from the remainder, so the panel's totals still equal the receipt's totals.

Usage::

    usage_summary_producer.py --receipt RECEIPT --out OUT
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

SCHEMA = "usage-summary.v1"
BRT = dt.timezone(dt.timedelta(hours=-3))
# receipt field -> v1 class. ``reasoning`` has no receipt field and is always null.
FIELDS = {
    "input": "fresh_input",
    "cacheWrite": "cache_write",
    "cacheRead": "cache_read",
    "output": "output",
}
# The four ADDITIVE classes partition a call's tokens. ``reasoning`` is a SUBSET of
# ``output`` (pi reports it inside output; dedupe-usage.py: "reasoning is not added to
# output"), so it must never be summed with them, and it stays null here because the
# receipt does not carry it. A consumer that stacks it beside ``output`` double-counts.
ADDITIVE = ("fresh_input", "cache_write", "cache_read", "output")
SUBSET_OF_OUTPUT = "reasoning"
CLASSES = (*ADDITIVE, SUBSET_OF_OUTPUT)
ROLE = {"assistant": "turn", "compaction": "compaction"}
# ponytail: one known case. pi records openai-codex cacheWrite as a literal 0 because
# the Responses API has no cache-creation counter; 0 there means "not reported".
# Extend this map when another provider is shown to fill a class with placeholder 0s.
NOT_REPORTED = {"openai-codex": frozenset({"cache_write"})}
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,95}$")  # == spec 380 validator
SCOPE_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4}) BRT")


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _epoch(value: object) -> float | None:
    """Same timestamp reading as dedupe-usage.py."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else value
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def window(receipt: dict, scanned_at: float) -> tuple[dt.datetime, dt.datetime]:
    """Coverage window: the scope's BRT day, ending at the scan (receipt mtime) if earlier."""
    m = SCOPE_DATE.search(str(receipt.get("scope", "")))
    if not m:
        raise ValueError("receipt scope names no DD/MM/YYYY BRT day; refusing to guess a window")
    day, month, year = (int(g) for g in m.groups())
    start = dt.datetime(year, month, day, tzinfo=BRT)
    end = min(start + dt.timedelta(days=1), dt.datetime.fromtimestamp(scanned_at, BRT))
    if end < start:
        raise ValueError("receipt was written before its own window starts")
    return start, end


def _label(value: str) -> str:
    if not LABEL.match(value):
        raise ValueError("a provider/model label does not fit the v1 label pattern")
    return value


def _counts(src: dict, prefix: str, provider: str, unknown: set[str]) -> dict:
    """v1 class counts from receipt fields ``<prefix><field>``; None where unknown."""
    out: dict[str, int | None] = {}
    for field, cls in FIELDS.items():
        value = src.get(prefix + field, 0)
        bad = cls in unknown or cls in NOT_REPORTED.get(provider, ())
        out[cls] = None if bad or not isinstance(value, int) else value
    out["reasoning"] = None
    return out


def file_models(path: Path, start: float, end: float) -> set[tuple[str, str]] | None:
    """Every (provider, model) behind the file's in-window usage rows; None if unreadable.

    Mirrors dedupe-usage.py's ``active`` tracking. Reads metadata fields only.
    """
    active = ("unknown", "unknown")
    seen: set[tuple[str, str]] = set()
    try:
        with path.open("rb") as stream:
            for raw in stream:
                try:
                    entry = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                msg = entry.get("message") or {}
                kind = entry.get("type")
                if kind == "model_change":
                    active = (entry.get("provider", "unknown"), entry.get("modelId", "unknown"))
                is_turn = kind == "message" and msg.get("role") == "assistant"
                if is_turn:
                    active = (msg.get("provider", "unknown"), msg.get("model", "unknown"))
                if not (is_turn or kind == "compaction"):
                    continue
                ts = _epoch(entry.get("timestamp")) or _epoch(msg.get("timestamp"))
                if ts is not None and start <= ts < end:
                    seen.add(active)
    except OSError:
        return None
    return seen


def _pane(binding: list) -> str | None:
    if len(binding) != 1:
        return None  # unbound or ambiguous: stays unattributed
    label = f"{binding[0].get('session')}/{binding[0].get('pane')}"
    return label if LABEL.match(label) else None


def build(receipt: dict, scanned_at: float, now: dt.datetime, report: dict | None = None) -> dict:
    """The v1 document. ``generated_at`` is the SOURCE as-of (= ``window_end``), not the
    write time: v1 has an exact key set and the panel ages ``generated_at``, so re-running
    on an old receipt must still read as old. The write time is the file's own mtime.
    ``report`` (optional) receives ``rejected_attributions``: groups whose attributed
    rows exceeded the receipt totals and were therefore left unattributed.
    """
    start, end = window(receipt, scanned_at)
    if end > now:
        raise ValueError("source as-of is in the future; refusing to emit a fresh-looking file")
    rejected = 0
    groups: dict[tuple[str, str, str], dict] = {}
    for key, src in receipt["totals"].items():
        kind, provider, model = key.split("/", 2)
        if kind not in ROLE:
            raise ValueError("receipt has an unknown usage kind")
        unknown = {FIELDS[f[8:]] for f, v in src.items() if f.startswith("unknown_") and v}
        groups[(kind, _label(provider), _label(model))] = {
            "requests": src.get("calls"),
            "counts": _counts(src, "", provider, unknown),
        }

    attributed: dict[tuple, dict] = {}
    for top in receipt.get("top_files_by_cache_write", []):
        pane = _pane(top.get("known_panes_before_migration") or [])
        if pane is None:
            continue
        models = file_models(Path(top["source"]), start.timestamp(), end.timestamp())
        if not models or len(models) != 1:
            continue
        provider, model = next(iter(models))
        for kind in ROLE:
            group = groups.get((kind, provider, model))
            calls = top.get(f"{kind}_calls", 0)
            if not calls or group is None:
                continue
            unknown = {c for c, v in group["counts"].items() if v is None}
            row = attributed.setdefault(
                (kind, provider, model, pane), {"requests": 0, "counts": dict.fromkeys(CLASSES, 0)}
            )
            row["requests"] += calls
            for cls, v in _counts(top, f"{kind}_", provider, unknown).items():
                row["counts"][cls] = None if v is None else (row["counts"][cls] or 0) + v

    rows = []
    for (kind, provider, model), group in sorted(groups.items()):
        mine = {k: v for k, v in attributed.items() if k[:3] == (kind, provider, model)}
        rest = {"requests": group["requests"], "counts": dict(group["counts"])}
        for row in mine.values():
            rest["requests"] -= row["requests"]
            for cls, v in row["counts"].items():
                if v is not None and rest["counts"][cls] is not None:
                    rest["counts"][cls] -= v
        if rest["requests"] < 0 or any(v is not None and v < 0 for v in rest["counts"].values()):
            # attributions exceed the totals: reject them, keep the receipt's group whole
            rejected += len(mine)
            mine, rest = {}, group
        for (_, _, _, pane), row in sorted(mine.items()):
            rows.append(_row(provider, model, pane, ROLE[kind], row))
        if rest["requests"]:
            rows.append(_row(provider, model, None, ROLE[kind], rest))

    if report is not None:
        report["rejected_attributions"] = rejected
    return {
        "schema": SCHEMA,
        "generated_at": _iso(end),
        "coverage": {
            "window_start": _iso(start),
            "window_end": _iso(end),
            "files_scanned": receipt["counts"]["files_scanned"],
            # dedupe-usage.py has no skip path: an unparseable FILE aborts the scan,
            # and bad rows inside a file are counted as rows, not files.
            "files_skipped": receipt["counts"].get("files_skipped", 0),
        },
        "rows": rows,
    }


def _row(provider: str, model: str, pane: str | None, role: str, data: dict) -> dict:
    return {
        "provider": provider,
        "model": model,
        "pane": pane,
        "role": role,
        "requests": data["requests"],
        **{c: data["counts"][c] for c in CLASSES},
    }


def write_private(path: Path, doc: dict) -> None:
    """Atomic write, file 0600 and a 0700 parent that this call creates."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--receipt", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    report: dict = {}
    doc = build(receipt, args.receipt.stat().st_mtime, dt.datetime.now(dt.UTC), report)
    write_private(args.out, doc)
    attributed = sum(r["pane"] is not None for r in doc["rows"])
    print(
        f"wrote {SCHEMA}: {len(doc['rows'])} rows ({attributed} pane-attributed, "
        f"{report['rejected_attributions']} attributions rejected), as-of {doc['generated_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
