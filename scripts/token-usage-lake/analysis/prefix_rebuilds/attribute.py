#!/usr/bin/env python3
"""Attribute the PiCommons profiler's prefix-rebuild candidates to causes.

Analysis only, stdlib only, metadata only. The candidate rule is NOT re-implemented:
the pinned ``profile_usage.Profile`` is imported (sha256 checked) and its
``prefix_candidate`` is wrapped to record, per candidate, its time, the previous
same-model call's time, the profiler's own cause label, cache_write and the
cacheWrite1h subset. Nothing else about a row is kept.

Each candidate is then joined to gateway journal METADATA lines:
  * ``affinity-migrate`` lines (time only) -> rule (a): a migrate in the 5 s before;
  * ``done ... /v1/messages`` lines (time, model, seat, bid hash) -> the account that
    served the candidate and the account that served the previous call, by the
    Root cold-misses join (same model, |dt| <= 2 s, the file's learned seat, unique).

Classes for each candidate (profiler label kept alongside):
  (a)  account_switch_migrate     an affinity-migrate within 5 s before the call
  (a') account_switch_joined      joined account != previous call's joined account
  (b)  idle_past_ttl              gap > the write's TTL: 3600 s if it had a 1h part, else 300 s
  (c)  prompt_or_tool_churn       UNMEASURABLE: no system/tool hash in session rows
  (d)  unexplained                none of the above

Output: aggregates only (counts, token sums). Raw bid/seat/session values live in
memory under an ephemeral HMAC alias and are never written.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import hashlib
import hmac
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

PROFILER_SHA256 = "5e62c5214ab6a81725e8ecf430ab565868f67dfb93c39ac116686b12964de98c"
MIGRATE_WINDOW_S = 5.0
JOIN_S = 2.0
TTL_5M_S = 300
TTL_1H_S = 3600
SHIFT_CONTROL_S = 600.0
_TS = r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?[+-]\d\d:\d\d)"
MIGRATE = re.compile(_TS + r" .*\] affinity-migrate ")
DONE = re.compile(
    _TS + r" .*\] done\s+method=POST path=/v1/messages status=\d+ bid=(\S+) .*?"
    r"model=(\S+) .*?seat=(\S+)"
)
# Names that would carry a system-prompt or tool-list hash/size if pi recorded one.
CHURN_KEY = re.compile(r"(system|tool).*(hash|sha|digest|bytes|size|len)", re.I)


def load_profiler(path: Path):
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != PROFILER_SHA256:
        raise SystemExit("profile_usage.py is not the pinned rev-2 file; refusing")
    spec = importlib.util.spec_from_file_location("profile_usage", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.resolve().parents[1] / "capture"))
    spec.loader.exec_module(mod)
    return mod


def epoch(value: str) -> float:
    return dt.datetime.fromisoformat(value).timestamp()


class Journal:
    """Metadata index of gateway lines. Raw values are aliased on read."""

    def __init__(self, lines, key: bytes):
        self.migrates: list[float] = []
        self.done: dict[str, list[tuple[float, str, str]]] = collections.defaultdict(list)
        self._key = key
        for line in lines:
            if m := MIGRATE.match(line):
                self.migrates.append(epoch(m[1]))
            elif m := DONE.match(line):
                # (time, account alias, seat alias); groups: 1 ts, 2 bid, 3 model, 4 seat
                self.done[m[3]].append((epoch(m[1]), self.alias(m[2]), self.alias(m[4])))
        self.migrates.sort()
        for rows in self.done.values():
            rows.sort()

    def alias(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:12]

    def migrate_before(self, t: float) -> bool:
        i = bisect.bisect_left(self.migrates, t - MIGRATE_WINDOW_S)
        return i < len(self.migrates) and self.migrates[i] <= t

    def near(self, model: str, t: float) -> list[tuple[float, str, str]]:
        rows = self.done.get(model, [])
        i = bisect.bisect_left(rows, (t - JOIN_S,))
        out = []
        while i < len(rows) and rows[i][0] <= t + JOIN_S:
            out.append(rows[i])
            i += 1
        return out


def make_profile(mod, key: bytes, since: float, until: float):
    class Recorder(mod.Profile):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.records: list[dict] = []
            self.calls: list[tuple[str, str, float]] = []  # (file alias, model, when)
            self.row_keys: collections.Counter = collections.Counter()

        def add_file(self, path):
            self._file = self.alias("file", str(path))
            self._base = Path(path).name  # memory only; emitted solely in --falsifiers
            return super().add_file(path)

        def row(self, r, state):
            keys = set(r) | {
                f"message.{k}"
                for k in (r.get("message") or {})
                if isinstance(r.get("message"), dict)
            }
            self.row_keys.update(k for k in keys if CHURN_KEY.search(k))
            self.row_keys["rows_checked"] += 1
            return super().row(r, state)

        def prefix_candidate(self, active, usage, when, state):
            last = state["last"].get(active)
            before = dict(self.prefix)
            super().prefix_candidate(active, usage, when, state)
            self.calls.append((self._file, active[1], when))
            grown = [k for k, v in self.prefix.items() if v != before.get(k, 0)]
            cause = next(
                (k[len("candidates_") :] for k in grown if k.startswith("candidates_")), None
            )
            if cause is None:
                return
            w1h = mod.number(usage.get("cacheWrite1h"))
            self.records.append(
                {
                    "file": self._file,
                    "base": self._base,
                    "provider": active[0],
                    "model": active[1],
                    "when": when,
                    "prev": last[1],
                    "profiler_cause": cause,
                    "cache_write": mod.number(usage.get("cacheWrite")) or 0,
                    "cache_write_1h": w1h,
                }
            )

    return Recorder(key, since=since, until=until)


def learn_seats(calls, journal: Journal) -> dict[str, str]:
    """Root's stage 1: a file's seat = the seat of its globally unique matches."""
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for file, model, when in calls:
        hits = journal.near(model, when)
        if len(hits) == 1:
            votes[file][hits[0][2]] += 1
    return {
        f: c.most_common(1)[0][0]
        for f, c in votes.items()
        if len(c) == 1 or c.most_common(2)[1][1] * 10 < c.most_common(1)[0][1]
    }


def account(journal: Journal, seats, file, model, when):
    seat = seats.get(file)
    if seat is None:
        return None
    hits = [h for h in journal.near(model, when) if h[2] == seat]
    return hits[0][1] if len(hits) == 1 else None


def joined_state(rec, journal: Journal, seats) -> str:
    now = account(journal, seats, rec["file"], rec["model"], rec["when"])
    prev = account(journal, seats, rec["file"], rec["model"], rec["prev"])
    return "unjoined" if now is None or prev is None else "switch" if now != prev else "same"


def classify(rec, journal: Journal, seats, shift: float = 0.0) -> str:
    t = rec["when"] + shift
    if journal.migrate_before(t):
        return "a_account_switch_migrate"
    now = account(journal, seats, rec["file"], rec["model"], t)
    prev = account(journal, seats, rec["file"], rec["model"], rec["prev"] + shift)
    if now is not None and prev is not None and now != prev:
        return "a2_account_switch_joined"
    if rec["when"] - rec["prev"] > ttl(rec):
        return "b_idle_past_ttl"
    if now is None or prev is None:
        return "d_unexplained_unjoined"
    return "d_unexplained_same_account"


def ttl(rec) -> int:
    """TTL-aware: a write with a cacheWrite1h part lives 1 h, otherwise 5 min."""
    return TTL_1H_S if rec["cache_write_1h"] else TTL_5M_S


def side_of(rec, cut: float) -> str:
    # A post candidate whose previous call predates the cut spans the restart:
    # the restart emptied the in-process session pins, so its route was a fresh pick.
    return "pre" if rec["when"] < cut else "post_spans_restart" if rec["prev"] < cut else "post"


def falsifiers(records, journal: Journal, seats, cut: float) -> list[dict]:
    """TRUE falsifier cases: both calls in the post-restart process, and the candidate is an
    account switch (any profiler label) or still unexplained (profiler label 'unexplained').
    Basename + timestamps only, for the operator; never part of the aggregate document."""
    out = []
    for rec in records:
        if side_of(rec, cut) != "post":
            continue
        cls = classify(rec, journal, seats)
        switch = cls.startswith("a")
        if not (switch or (rec["profiler_cause"] == "unexplained" and cls.startswith("d"))):
            continue
        brt = dt.timezone(dt.timedelta(hours=-3))
        joined = joined_state(rec, journal, seats)
        bucket = (
            "idle_past_ttl_overdetermined"
            if rec["when"] - rec["prev"] > ttl(rec)
            else "account_switch_by_join"
            if joined == "switch"
            else "account_switch_migrate_only"
            if switch
            else "unexplained"
        )
        out.append(
            {
                "session_file": rec["base"],
                "call_at": dt.datetime.fromtimestamp(rec["when"], brt).isoformat(
                    timespec="seconds"
                ),
                "previous_call_at": dt.datetime.fromtimestamp(rec["prev"], brt).isoformat(
                    timespec="seconds"
                ),
                "bucket": bucket,
                "profiler_cause": rec["profiler_cause"],
            }
        )
    return sorted(out, key=lambda x: x["call_at"])


def merge(docs: list[dict]) -> dict:
    """Sum aggregate documents of disjoint file chunks; equal strings are kept."""

    def add(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            return {
                k: add(a[k], b[k]) if k in a and k in b else a.get(k, b.get(k))
                for k in a.keys() | b.keys()
            }
        if isinstance(a, bool) or isinstance(b, bool):
            return a or b
        if isinstance(a, int) and isinstance(b, int):
            return a + b
        if isinstance(a, list) and isinstance(b, list):
            return sorted(set(a) | set(b))
        if a != b:
            raise ValueError("chunks disagree on a non-additive field")
        return a

    out = docs[0]
    for doc in docs[1:]:
        out = add(out, doc)
    return out


def summarize(records, journal: Journal, seats, cut: float, churn_keys: dict) -> dict:
    out: dict = {}
    for rec in records:
        side = side_of(rec, cut)
        for shift_name, shift in (("real", 0.0), ("shift_600s_control", SHIFT_CONTROL_S)):
            if shift and rec["profiler_cause"] != "unexplained":
                continue
            cls = classify(rec, journal, seats, shift)
            slot = out.setdefault(rec["profiler_cause"], {}).setdefault(side, {})
            slot = slot.setdefault(shift_name, collections.Counter())
            slot[cls] += 1
            slot[cls + ".cache_write"] += rec["cache_write"]
        cross = out[rec["profiler_cause"]][side].setdefault("cross", collections.Counter())
        cross[
            f"migrate5s={int(journal.migrate_before(rec['when']))}"
            f",joined={joined_state(rec, journal, seats)}"
        ] += 1
        tally = out[rec["profiler_cause"]][side].setdefault("gap_s", collections.Counter())
        gap = rec["when"] - rec["prev"]
        tally["le_300" if gap <= 300 else "le_3600" if gap <= 3600 else "gt_3600"] += 1
        tally["write_has_1h_part"] += bool(rec["cache_write_1h"])
        tally["gap_past_ttl"] += gap > ttl(rec)
    return {
        "by_profiler_cause": {
            c: {s: {k: dict(v) for k, v in d.items()} for s, d in sides.items()}
            for c, sides in out.items()
        },
        "candidates": len(records),
        "c_prompt_or_tool_churn": {
            "measurable": any(k != "rows_checked" for k in churn_keys),
            "rows_checked": churn_keys.get("rows_checked", 0),
            "matching_key_names": sorted(k for k in churn_keys if k != "rows_checked"),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--merge", type=Path, nargs="+", help="sum chunk outputs into --out")
    ap.add_argument("--profiler", type=Path)
    ap.add_argument("--root", type=Path)
    ap.add_argument("--files-from", type=Path, help="newline list; replaces --root/--min-mtime")
    ap.add_argument("--journal", type=Path, help="journalctl -o short-iso-precise")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--min-mtime", help="read only files touched since")
    ap.add_argument("--cut", help="PRE/POST split (gateway start)")
    ap.add_argument("--falsifiers", type=Path, help="write TRUE-falsifier cases (basenames)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.merge:
        _write(args.out, merge([json.loads(p.read_text()) for p in args.merge]))
        return 0
    key = os.urandom(32)  # ephemeral: aliases are join-only and die with the process
    mod = load_profiler(args.profiler)
    prof = make_profile(mod, key, epoch(args.since), epoch(args.until))
    if args.files_from:
        files = [Path(x) for x in args.files_from.read_text().splitlines() if x]
    else:
        floor = epoch(args.min_mtime)
        files = [p for p in sorted(args.root.rglob("*.jsonl")) if p.stat().st_mtime >= floor]
    for path in files:
        prof.add_file(path)
    with args.journal.open(encoding="utf-8", errors="replace") as fh:
        journal = Journal(fh, key)
    seats = learn_seats(prof.calls, journal)
    doc = summarize(prof.records, journal, seats, epoch(args.cut), prof.row_keys)
    doc["coverage"] = {
        "files_read": len(files),
        "profiler_prefix_counts_on_these_files": dict(prof.prefix),
        "files_with_learned_seat": len(seats),
        "journal_migrate_lines": len(journal.migrates),
        "journal_done_lines": sum(map(len, journal.done.values())),
        "since": args.since,
        "until": args.until,
        "cut": args.cut,
        "min_mtime": args.min_mtime,
    }
    if args.falsifiers:
        _write(args.falsifiers, falsifiers(prof.records, journal, seats, epoch(args.cut)))
    print(json.dumps({"out_sha256": _write(args.out, doc), **doc["coverage"]}))
    return 0


def _write(path: Path, doc) -> str:
    data = json.dumps(doc, indent=1, sort_keys=True).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
