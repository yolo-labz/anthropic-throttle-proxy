#!/usr/bin/env python3
"""Emit ``usage-summary.v1`` (spec 380) straight from pi session transcripts.

The dashboard's Usage panel has always been dark on this host: nothing wrote the
document. The lake's other producer needs a receipt from ``dedupe-usage.py``,
which lives in a study scratch dir and not in this repo, so in practice the
panel started empty and stayed empty — nobody could see where the tokens went.

pi writes one JSONL transcript per session under
``~/.pi/agent/sessions/<cwd-slug>/``. Every ``message`` entry that finished a
model call carries the provider, the model and a ``usage`` block
(``input``/``output``/``cacheRead``/``cacheWrite``/``reasoning``), and a
``compaction`` entry marks a context rewrite. That is the whole input here: no
prompt text, no tool arguments, no file paths beyond the session directory.

Attribution is by ``(provider, model, pane, role)``. ``pane`` is the seat name
the session gave itself (``session_info.name``), which is what herdr titles the
pane with; a session that never named itself, or named itself with something
that is not label-shaped, contributes an unattributed row rather than a guessed
one. ``role`` is ``turn`` for ordinary calls and ``compaction`` for the call
that produced a ``compaction`` entry (the last usage-bearing message within
``COMPACTION_WINDOW_S`` before that entry).

Honesty rules (the panel renders ``null`` as *unknown*, never zero):
  * every token class stays its own field; nothing is folded together;
  * a class that any contributing message failed to report is ``null`` for that
    row — a partially measured row must not masquerade as a small one;
  * bytes outside the window are dropped, and ``coverage`` says which window and
    how many files were read or skipped.

Usage::

    usage_summary_from_sessions.py --out ~/.local/state/throttle-lanes/usage-summary.json
    usage_summary_from_sessions.py --window-hours 5 --out - --validate

``--validate`` runs the shipped validator
(``anthropic_throttle_proxy.usage_summary``) over the document before writing,
so a producer bug is a producer error here rather than an "invalid summary" line
on the dashboard.
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
ROLES = ("turn", "compaction")
# pi's usage block -> the document's class name. ``reasoning`` is pi-native.
FIELDS = {
    "input": "fresh_input",
    "cacheWrite": "cache_write",
    "cacheRead": "cache_read",
    "output": "output",
    "reasoning": "reasoning",
}
CLASSES = tuple(FIELDS.values())
# A summariser call starts right before the entry it produces; anything older
# belongs to the conversation that was being summarised.
COMPACTION_WINDOW_S = 600
# Same shape the validator enforces: no spaces, no '@', so no prompt text or
# e-mail address can ride out in a label.
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,95}$")
DEFAULT_SESSIONS = "~/.pi/agent/sessions"


def _label(value: object) -> str | None:
    return value if isinstance(value, str) and _LABEL.match(value) else None


def _when(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _usage(block: object) -> dict[str, int | None]:
    """One message's counts. A missing or non-integer key is unknown (None)."""
    out: dict[str, int | None] = {}
    for src, dest in FIELDS.items():
        value = block.get(src) if isinstance(block, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            out[dest] = None
        else:
            out[dest] = value
    return out


class FileScan:
    """The usage-bearing messages of one transcript, plus its compaction marks."""

    def __init__(self) -> None:
        self.calls: list[tuple[dt.datetime, str, str, dict[str, int | None]]] = []
        self.compactions: list[dt.datetime] = []
        self.name: str | None = None
        self.last_model: tuple[str, str] | None = None

    def feed(self, line: str, window_start: dt.datetime, window_end: dt.datetime) -> None:
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        when = _when(entry.get("timestamp"))
        if when is None:
            return
        kind = entry.get("type")
        if kind == "session_info":
            self.name = _label(entry.get("name")) or self.name
            return
        if kind == "model_change":
            provider, model = _label(entry.get("provider")), _label(entry.get("modelId"))
            if provider and model:
                self.last_model = (provider, model)
            return
        if kind == "compaction":
            if window_start <= when <= window_end:
                self.compactions.append(when)
            return
        if kind != "message":
            return
        message = entry.get("message")
        if not isinstance(message, dict) or "usage" not in message:
            return
        if not (window_start <= when <= window_end):
            return
        provider = _label(message.get("provider"))
        model = _label(message.get("model"))
        if not (provider and model):
            if self.last_model is None:
                return
            provider, model = self.last_model
        self.calls.append((when, provider, model, _usage(message.get("usage"))))

    def rows(self) -> list[tuple[str, str, str | None, str, dict[str, int | None]]]:
        """``(provider, model, pane, role, counts)`` per call, compaction-aware."""
        marks = sorted(self.compactions)
        taken: set[int] = set()
        roles: dict[int, str] = {}
        for mark in marks:
            best = None
            for index, (when, _p, _m, _c) in enumerate(self.calls):
                if index in taken:
                    continue
                if when > mark:
                    break
                if (mark - when).total_seconds() <= COMPACTION_WINDOW_S:
                    best = index
            if best is not None:
                taken.add(best)
                roles[best] = "compaction"
        return [
            (provider, model, self.name, roles.get(index, "turn"), counts)
            for index, (_when, provider, model, counts) in enumerate(self.calls)
        ]


class _Bucket:
    """One document row under construction: counts plus what stayed unknown."""

    __slots__ = ("requests", "counts", "unknown")

    def __init__(self) -> None:
        self.requests = 0
        self.counts: dict[str, int] = {cls: 0 for cls in CLASSES}
        self.unknown: set[str] = set()

    def add(self, counts: dict[str, int | None]) -> None:
        self.requests += 1
        for cls in CLASSES:
            value = counts.get(cls)
            if value is None:
                self.unknown.add(cls)
            elif cls not in self.unknown:
                self.counts[cls] += value

    def row(self, provider: str, model: str, pane: str | None, role: str) -> dict[str, object]:
        out: dict[str, object] = {
            "provider": provider,
            "model": model,
            "pane": pane,
            "role": role,
            "requests": self.requests,
        }
        for cls in CLASSES:
            out[cls] = None if cls in self.unknown else self.counts[cls]
        return out


def _merge(
    rows: list[tuple[str, str, str | None, str, dict[str, int | None]]],
) -> list[dict[str, object]]:
    """Fold per-call rows into the document's one-row-per-key shape."""
    buckets: dict[tuple[str, str, str | None, str], _Bucket] = {}
    for provider, model, pane, role, counts in rows:
        buckets.setdefault((provider, model, pane, role), _Bucket()).add(counts)
    return [
        bucket.row(*key)
        for key, bucket in sorted(
            buckets.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "", item[0][3])
        )
    ]


def scan(
    roots: list[Path], window_start: dt.datetime, window_end: dt.datetime
) -> tuple[list[dict[str, object]], int, int]:
    """Walk every transcript; return (rows, files_scanned, files_skipped)."""
    merged: list[tuple[str, str, str | None, str, dict[str, int | None]]] = []
    scanned = skipped = 0
    for root in roots:
        for path in sorted(root.rglob("*.jsonl")):
            try:
                if dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC) < window_start:
                    continue  # written before the window opened
                transcript = FileScan()
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        transcript.feed(line, window_start, window_end)
            except OSError:
                skipped += 1
                continue
            scanned += 1
            merged.extend(transcript.rows())
    return _merge(merged), scanned, skipped


def build(
    roots: list[Path],
    window_hours: float,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    now = now or dt.datetime.now(dt.UTC)
    window_start = now - dt.timedelta(hours=window_hours)
    rows, scanned, skipped = scan(roots, window_start, now)
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "coverage": {
            "window_start": window_start.isoformat().replace("+00:00", "Z"),
            "window_end": now.isoformat().replace("+00:00", "Z"),
            "files_scanned": scanned,
            "files_skipped": skipped,
        },
        "rows": rows,
    }


def validate(doc: dict[str, object]) -> str | None:
    """Run the shipped validator. Returns a message when it cannot be imported."""
    try:
        from anthropic_throttle_proxy import usage_summary
    except ImportError:
        return "validator not importable (run through the project venv to validate)"
    summary = usage_summary.parse(doc)
    if summary.rejected_rows:
        raise SystemExit(f"producer bug: {summary.rejected_rows} row(s) rejected")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sessions-dir",
        action="append",
        default=None,
        help=f"transcript root (repeatable, default {DEFAULT_SESSIONS})",
    )
    parser.add_argument("--window-hours", type=float, default=168.0)
    parser.add_argument("--out", required=True, help="output path, or - for stdout")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)

    roots = [Path(os.path.expanduser(raw)) for raw in (args.sessions_dir or [DEFAULT_SESSIONS])]
    roots = [root for root in roots if root.is_dir()]
    if not roots:
        print("usage_summary_from_sessions: no transcript root found", file=sys.stderr)
        return 2

    doc = build(roots, args.window_hours)
    if args.validate:
        note = validate(doc)
        if note:
            print(f"usage_summary_from_sessions: {note}", file=sys.stderr)
    blob = json.dumps(doc, indent=1, sort_keys=False) + "\n"
    if args.out == "-":
        sys.stdout.write(blob)
        return 0
    target = Path(os.path.expanduser(args.out))
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: the dashboard polls this path and must never read a torn file.
    scratch = target.with_name(target.name + ".tmp")
    scratch.write_text(blob, encoding="utf-8")
    scratch.replace(target)
    print(f"usage_summary_from_sessions: wrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
