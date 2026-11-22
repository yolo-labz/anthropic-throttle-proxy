#!/usr/bin/env python3
"""Price session-affinity hops and replay pin-release rules (row affinity-saturation-hops).

Analysis only, stdlib only, metadata only. Inputs:
  * a gateway journal extract (``journalctl -o short-iso-precise``) of ONE gateway process:
    ``body_passthrough``, ``affinity-migrate``, ``account-route``, ``queue+``, ``done``,
    ``bearer-new`` and ``aimd-*`` lines;
  * the gateway usage ledger (``THROTTLE_USAGE_LEDGER=observe``): epoch, bearer hash, model,
    hashed session key and usage counters per request.

The ledger's ``session`` is the same ``session_affinity`` key the migrate line logs, so a hop
joins to that session's next call exactly, with no time-window heuristic.

Per hop (pinned account A -> sibling B at t):
  * hop call     the session's first ledger row on B at or after t;
  * prev call    the session's last ledger row before it;
  * attributable the part of the previous prompt the hop call did not read from cache, capped by
                 what it wrote: min(cacheWrite, max(0, prompt_prev - cacheRead));
  * rebuilt      attributable >= half the previous prompt;
  * wait avoided A's next admit had the request stayed: the (Q_A + 1)-th slot release on A after
                 t, Q_A = A's requests parked at t (dispatch = done - elapsed_ms, proxy.py t0);
  * est wait     the limiter's own drain estimate for A at t (``_service_estimate`` and
                 ``_compute_drain`` from a pinned source tree), fed A's reconstructed holds,
                 its last 64 completions and ``ahead`` = distinct parked clients, which is what
                 ``drain_estimate(client_id=None)`` counts. That is what a rule can know before
                 routing; rule (a) keeps the pin only on an EVIDENCED estimate <= T, as the PR.

Bearer hashes, connection ids, seats and projects never leave memory.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from attribute import _write

BRT = dt.timezone(dt.timedelta(hours=-3))
LINE_RE = re.compile(r"^(\S+) \S+ \S+ \[anthropic-throttle\] (\S+)\s+(.*)$")
KV_RE = re.compile(r"(\w+)=(\S+)")
SAMPLES = 64  # limiter._SERVICE_SAMPLES
RETURN_WINDOW_S = 3600
DEFAULT_CAP = 6  # THROTTLE_AIMD_INITIAL_CONCURRENT on the host
INFLIGHT_BUCKETS = ((0, 9), (10, 19), (20, 29), (30, 35), (36, 40), (41, 10**6))


def epoch(text: str) -> float:
    return dt.datetime.fromisoformat(text).timestamp()


def iso(t: float | None) -> str | None:
    return None if t is None else dt.datetime.fromtimestamp(t, BRT).isoformat(timespec="seconds")


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int(q * len(ordered) + 0.5) - 1))], 1)


def parse_journal(lines) -> tuple[list[dict], list[dict], dict]:
    """Requests (queue+ .. done), migrates and per-bearer AIMD caps, in log order."""
    requests: list[dict] = []
    migrates: list[dict] = []
    caps: dict[str, list[tuple[float, int]]] = collections.defaultdict(list)
    open_: dict[tuple, collections.deque] = collections.defaultdict(collections.deque)
    pending: dict | None = None
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        t, kind, kv = epoch(m[1]), m[2], dict(KV_RE.findall(m[3]))
        if kind == "body_passthrough":
            pending = {"bytes": int(kv.get("bytes") or 0), "migrate": None}
        elif kind == "affinity-migrate":
            migrates.append(
                {
                    "t": t,
                    "session": kv.get("session"),
                    "src": kv.get("from"),
                    "dst": kv.get("to"),
                    "req": None,
                }
            )
            if pending is not None:
                pending["migrate"] = len(migrates) - 1
        elif kind == "queue+" and kv.get("path") == "/v1/messages":
            pend = pending or {}
            req = {
                "t_q": t,
                "bid": kv.get("bid"),
                "cid": kv.get("cid"),
                "inflight": int(kv.get("inflight") or 0),
                "bytes": pend.get("bytes"),
                "migrate": pend.get("migrate"),
                "dispatch": None,
                "done": None,
                "elapsed": None,
            }
            if req["migrate"] is not None:
                migrates[req["migrate"]]["req"] = len(requests)
            open_[(req["bid"], req["cid"])].append(len(requests))
            requests.append(req)
            pending = None
        elif kind == "done" and kv.get("path") == "/v1/messages":
            queue = open_.get((kv.get("bid"), kv.get("cid")))
            if not queue:  # re-routed mid-flight: the connection alone identifies it
                queue = next((q for (_, c), q in open_.items() if c == kv.get("cid") and q), None)
            if not queue:
                continue
            req = requests[queue.popleft()]
            req["done"], req["elapsed"] = t, int(kv.get("elapsed_ms") or 0) / 1000
            req["dispatch"] = t - req["elapsed"]
        elif kind in ("bearer-new", "aimd-grow", "aimd-shrink") and "max_concurrent" in kv:
            caps[kv.get("bid")].append((t, int(kv["max_concurrent"])))
    return requests, migrates, caps


def cap_at(caps: dict, bid: str, t: float) -> int:
    cap = DEFAULT_CAP
    for stamp, value in caps.get(bid, ()):
        if stamp > t:
            break
        cap = value
    return cap


def lane_at(done_sorted: list[dict], t: float) -> tuple[list[float], int, list[float]]:
    """(hold starts, parked count, parked clients, last completions) for one bearer at t."""
    holds = [r["dispatch"] for r in done_sorted if r["dispatch"] <= t < r["done"]]
    parked = [r for r in done_sorted if r["t_q"] <= t < r["dispatch"]]
    finished = [r["elapsed"] for r in done_sorted if r["done"] <= t][-SAMPLES:]
    return holds, len(parked), len({r["cid"] for r in parked}), finished


def wait_if_stayed(dones: list[float], t: float, holds: list, parked: int, cap: int):
    if parked == 0 and len(holds) < cap:
        return 0.0
    k = bisect.bisect_right(dones, t) + parked
    return dones[k] - t if k < len(dones) else None


def tokens(row: dict | None) -> dict | None:
    keys = ("input", "cacheRead", "cacheWrite", "output")
    if row is None or row.get("usage_complete") is not True:
        return None
    if not all(isinstance(row.get(k), int) for k in keys):
        return None
    return {
        "input": row["input"],
        "cache_read": row["cacheRead"],
        "cache_write": row["cacheWrite"],
        "output": row["output"],
    }


def prompt(tok: dict) -> int:
    return tok["input"] + tok["cache_read"] + tok["cache_write"]


def join_ledger(rows: list[dict], t: float, dst: str) -> tuple[dict | None, dict | None]:
    hop = next((r for r in rows if r["bearer"] == dst and t - 1 <= r["epoch"] <= t + 900), None)
    prev = None
    for r in rows:
        if r is hop or r["epoch"] > t:
            break
        prev = r
    return hop, prev


def build(journal_lines, ledger_rows, prices: dict, *, since, until, cut, estimator) -> dict:
    requests, migrates, caps = parse_journal(journal_lines)
    by_bid: dict[str, list[dict]] = collections.defaultdict(list)
    for r in requests:
        if r["done"] is not None:
            by_bid[r["bid"]].append(r)
    for rs in by_bid.values():
        rs.sort(key=lambda r: r["done"])
    dones = {b: [r["done"] for r in rs] for b, rs in by_bid.items()}
    sessions: dict[str, list[dict]] = collections.defaultdict(list)
    for row in ledger_rows:
        sessions[row["session"]].append(row)
    for rows in sessions.values():
        rows.sort(key=lambda r: r["epoch"])

    hops, seen = [], collections.defaultdict(list)
    for mig in migrates:
        t = mig["t"]
        if not since <= t < until:
            continue
        src, dst, key = mig["src"], mig["dst"], mig["session"]
        holds, parked, clients, finished = lane_at(by_bid.get(src, []), t)
        cap = cap_at(caps, src, t)
        wait = wait_if_stayed(dones.get(src, []), t, holds, parked, cap)
        est, evidenced = estimator(holds, finished, cap, parked, clients, t)
        hop_row, prev_row = join_ledger(sessions.get(key, []), t, dst)
        hop_tok, prev_tok = tokens(hop_row), tokens(prev_row)
        attributable = rebuilt = delta = list_usd = None
        if hop_tok and prev_tok:
            attributable = min(
                hop_tok["cache_write"], max(0, prompt(prev_tok) - hop_tok["cache_read"])
            )
            rebuilt = prompt(prev_tok) > 0 and attributable >= 0.5 * prompt(prev_tok)
            price = prices.get(hop_row["model"])
            if price:
                list_usd = attributable * price[0] / 1e6
                delta = attributable * (price[0] - price[1]) / 1e6
        back = next(
            (
                p
                for p in reversed(seen[key])
                if p["src"] == dst and p["dst"] == src and 0 < t - p["t"] <= RETURN_WINDOW_S
            ),
            None,
        )
        seen[key].append(mig)
        req = requests[mig["req"]] if mig["req"] is not None else None
        hops.append(
            {
                "side": "clean" if t < cut else "after_pushback",
                "t": t,
                "session": key,
                "model": hop_row["model"] if hop_row else None,
                "bytes": req["bytes"] if req else None,
                "est_wait_s": est,
                "est_evidenced": evidenced,
                "wait_avoided_s": wait,
                "own_wait_s": (req["dispatch"] - req["t_q"]) if req and req["dispatch"] else None,
                "own_service_s": req["elapsed"] if req else None,
                "parked": parked,
                "busy": len(holds),
                "cap": cap,
                "reverses": back["t"] if back else None,
                "hop_tok": hop_tok,
                "prev_tok": prev_tok,
                "hop_at": hop_row and hop_row["epoch"],
                "prev_at": prev_row and prev_row["epoch"],
                "attributable": attributable,
                "rebuilt": rebuilt,
                "list_usd": list_usd,
                "delta_usd": delta,
            }
        )
    return {
        "hops": hops,
        "requests": requests,
        "ledger_rows": ledger_rows,
        "since": since,
        "until": until,
        "cut": cut,
    }


def describe(hops: list[dict]) -> dict:
    known = [h for h in hops if h["attributable"] is not None]
    waits = [h["wait_avoided_s"] for h in hops if h["wait_avoided_s"] is not None]
    priced = [h for h in known if h["delta_usd"] is not None]
    returns = [h for h in hops if h["reverses"] is not None]
    return {
        "hops": len(hops),
        "joined_with_usage": len(known),
        "est_unevidenced": sum(1 for h in hops if not h["est_evidenced"]),
        "rebuilt": sum(1 for h in known if h["rebuilt"]),
        "attributable_cache_write": sum(h["attributable"] for h in known),
        "list_usd_cache_write": round(sum(h["list_usd"] for h in priced), 2),
        "list_usd_same_tokens_as_reads": round(
            sum(h["list_usd"] - h["delta_usd"] for h in priced), 2
        ),
        "list_usd_delta": round(sum(h["delta_usd"] for h in priced), 2),
        "return_hops_within_1h": len(returns),
        "return_hops_rebuilt": sum(1 for h in returns if h["rebuilt"]),
        "wait_avoided_s": {
            "n": len(waits),
            "p50": quantile(waits, 0.5),
            "p95": quantile(waits, 0.95),
            "sum": round(sum(waits), 1),
            "zero": sum(1 for w in waits if w == 0),
        },
        "est_wait_s": {
            "p50": quantile([h["est_wait_s"] for h in hops if h["est_wait_s"] is not None], 0.5),
            "p95": quantile([h["est_wait_s"] for h in hops if h["est_wait_s"] is not None], 0.95),
        },
        "own_wait_on_sibling_s_p95": quantile(
            [h["own_wait_s"] for h in hops if h["own_wait_s"] is not None], 0.95
        ),
        "own_service_s_p50": quantile(
            [h["own_service_s"] for h in hops if h["own_service_s"] is not None], 0.5
        ),
        "body_bytes_quantiles": {
            q: quantile([h["bytes"] for h in hops if h["bytes"]], q) for q in (0.25, 0.5, 0.75)
        },
    }


def inflight_table(requests: list[dict], since: float, until: float) -> list[dict]:
    rows = []
    for lo, hi in INFLIGHT_BUCKETS:
        rs = [r for r in requests if since <= r["t_q"] < until and lo <= r["inflight"] <= hi]
        n_hop = sum(1 for r in rs if r["migrate"] is not None)
        rows.append(
            {
                "inflight": f"{lo}-{hi}" if hi < 10**6 else f">{lo - 1}",
                "requests": len(rs),
                "hops": n_hop,
                "per_100": round(100 * n_hop / len(rs), 2) if rs else None,
            }
        )
    return rows


def replay(hops: list[dict], waits_t=(2, 5, 10, 30, 60, 120), bytes_x=(65536, 262144, 1048576)):
    def row(name, avoided_if):
        avoided = [h for h in hops if avoided_if(h)]
        added = [h["wait_avoided_s"] for h in avoided if h["wait_avoided_s"] is not None]
        return {
            "rule": name,
            "hops_avoided": len(avoided),
            "share": round(len(avoided) / len(hops), 3) if hops else None,
            "added_wait_p50_s": quantile(added, 0.5),
            "added_wait_p95_s": quantile(added, 0.95),
            "added_wait_sum_s": round(sum(added), 1),
            "list_usd_saved": round(sum(h["delta_usd"] or 0 for h in avoided), 2),
            "cache_write_avoided": sum(h["attributable"] or 0 for h in avoided),
        }

    def short(h, t):
        return h["est_evidenced"] and h["est_wait_s"] is not None and h["est_wait_s"] <= t

    out = [row("never release on would-queue", lambda h: True)]
    out += [row(f"(a) stay unless est wait > {t}s", lambda h, t=t: short(h, t)) for t in waits_t]
    out += [
        row(
            f"(b) stay unless body <= {x // 1024} KiB",
            lambda h, x=x: h["bytes"] is not None and h["bytes"] > x,
        )
        for x in bytes_x
    ]
    out += [
        row(
            f"(c) stay unless est wait > {t}s and body <= {x // 1024} KiB",
            lambda h, t=t, x=x: short(h, t) or (h["bytes"] is not None and h["bytes"] > x),
        )
        for t in (10, 30, 60)
        for x in bytes_x
    ]
    return out


def public_hop(h: dict) -> dict:
    """hops.json row: session hash, timestamps and token counts only."""
    wait = h["wait_avoided_s"]
    return {
        "session": h["session"],
        "migrate_at": iso(h["t"]),
        "call_at": iso(h["hop_at"]),
        "prev_call_at": iso(h["prev_at"]),
        "pinned_next_admit_at": iso(h["t"] + wait) if wait is not None else None,
        "reverses_hop_at": iso(h["reverses"]),
        "hop_tokens": h["hop_tok"],
        "prev_tokens": h["prev_tok"],
        "attributable_cache_write": h["attributable"],
    }


def load_prices(models_json: Path, provider: str) -> dict:
    doc = json.loads(models_json.read_text())
    models = doc["providers"][provider]["models"]
    return {
        m["id"]: (m["cost"]["cacheWrite"], m["cost"]["cacheRead"])
        for m in models
        if isinstance(m.get("cost"), dict)
    }


def limiter_estimator(src: Path, max_wait: float):
    sys.path.insert(0, str(src))
    from anthropic_throttle_proxy import limiter

    def estimate(holds, finished, cap, parked, clients, now):
        service, residuals, count, source = limiter._service_estimate(
            collections.deque(finished, maxlen=SAMPLES), holds, now
        )
        drain = limiter._compute_drain(
            slots=cap,
            busy=len(holds),
            queued=parked,
            ahead=clients,
            service_time_s=service,
            residuals=residuals,
            samples=count,
            source=source,
            evidenced=source != "cold",
            max_wait=max_wait,
        )
        return drain.wait_s, drain.evidenced

    return estimate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--models", type=Path, required=True, help="pi models.json (list prices)")
    ap.add_argument("--provider", default="claude-lane")
    ap.add_argument("--limiter-src", type=Path, required=True, help="pinned src/ tree")
    ap.add_argument("--max-wait", type=float, default=240.0, help="THROTTLE_QUEUE_MAX_WAIT_S")
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--cut", required=True, help="first Retry-After pushback: clean window end")
    ap.add_argument("--hops-out", type=Path, required=True)
    ap.add_argument("--summary-out", type=Path, required=True)
    args = ap.parse_args(argv)
    since, until, cut = epoch(args.since), epoch(args.until), epoch(args.cut)
    ledger = [json.loads(x) for x in args.ledger.read_text().splitlines() if x.strip()]
    with args.journal.open() as fh:
        doc = build(
            fh,
            ledger,
            load_prices(args.models, args.provider),
            since=since,
            until=until,
            cut=cut,
            estimator=limiter_estimator(args.limiter_src, args.max_wait),
        )
    clean = [h for h in doc["hops"] if h["side"] == "clean"]
    writes = sum(r.get("cacheWrite") or 0 for r in ledger if since <= r["epoch"] < cut)
    summary = {
        "window": {"since": args.since, "until": args.until, "clean_until": args.cut},
        "clean": describe(clean),
        "after_pushback": describe([h for h in doc["hops"] if h["side"] != "clean"]),
        "clean_ledger_cache_write_total": writes,
        "by_model_clean": {
            str(m): dict(
                describe([h for h in clean if h["model"] == m]),
                ledger_cache_write=sum(
                    r.get("cacheWrite") or 0
                    for r in ledger
                    if since <= r["epoch"] < cut and r.get("model") == m
                ),
            )
            for m in sorted({h["model"] for h in clean}, key=str)
        },
        "hops_per_100_by_global_inflight_clean": inflight_table(doc["requests"], since, cut),
        "replay_clean": replay(clean),
    }
    hop_sha = _write(
        args.hops_out, {"window": summary["window"], "hops": [public_hop(h) for h in doc["hops"]]}
    )
    print(
        json.dumps(
            {
                "hops_sha256": hop_sha,
                "summary_sha256": _write(args.summary_out, summary),
                "hops": len(doc["hops"]),
                "clean": len(clean),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
