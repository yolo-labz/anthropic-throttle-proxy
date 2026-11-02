#!/usr/bin/env python3
"""Replay spec 377 weekly pacing over recorded unified 7d utilisation.

Every minute the replay calls the REAL pure decisions in
``anthropic_throttle_proxy.usage_allocator`` (``account_state``,
``decide_pacing``); only the glue that the proxy does around them (route the
spill, return the stamped 503) is re-expressed here.

Fluid model. Demand is a rate per account, learned from the quota meter log
(``quota-meter.jsonl``) as window-to-date slope. Routing splits the admitted
demand across the accounts the decision keeps. A refused non-priority unit
waits ``Retry-After * k`` seconds and re-enters demand (client retry loop).

Sim-only knobs, NOT production behaviour:
  * ``k``               multiplier on the honest Retry-After (client over-wait)
  * ``priority_share``  share of demand that is priority (no trace exists; assumed)
  * ``demand``          multiplier on the measured base rate
  * ``pi-prop``         proportional admission comparison column (research B rec 1)

Usage:
  uv run python scripts/pace_replay.py --scenario today --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any, NamedTuple

from anthropic_throttle_proxy import usage_allocator as ua

STEP_S = 60.0
WEEK_S = ua.WEEK_S
# Vault "General solution" L1: one warm Opus turn = 0.0030 % of the 7d window.
UNIT_PER_TURN = 3e-5
# THROTTLE_QUEUE_RETRY_AFTER_MAX_S default (config.py).
RETRY_CAP_S = 300
# ASSUMPTION: the meter log carries no priority trace; 20 % is a placeholder.
DEFAULT_PRIORITY_SHARE = 0.2
PI_RETRY_S = 60.0
BURSTS = {"1/56": 1 / 56, "1/28": 1 / 28, "1/14": 1 / 14}
KS = (1, 2, 3)
DEMANDS = (0.9, 1.0, 1.1)
BRT = dt.timezone(dt.timedelta(hours=-3))
DEFAULT_LOG = Path.home() / ".local/state/anthropic-throttle-proxy/quota-meter.jsonl"
# Brief scenario "today": 31/10/2026 13:30 BRT, gmail 14 % @ 6d4h, proton 17 % @ 5d17h.
TODAY_AS_OF = dt.datetime(2026, 10, 31, 13, 30, tzinfo=BRT)
DEFAULT_ACCOUNTS = {"gmail": "666a53af", "proton": "b144f62f"}


def read_meter(path: Path, as_of: float, bearers: dict[str, str]) -> list[dict[str, Any]]:
    """Per account: last 7d reading <= as_of and its window-to-date base rate (util/s)."""
    rows: dict[str, list[dict[str, Any]]] = {b: [] for b in bearers.values()}
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            bid = row.get("bearer")
            if bid in rows and row.get("util_7d") is not None and row["epoch"] <= as_of:
                rows[bid].append(row)
    out = []
    for label, bid in bearers.items():
        series = rows[bid]
        if not series:
            raise SystemExit(f"no 7d samples for {label} ({bid}) at or before as_of")
        last = series[-1]
        same = [r for r in series if r["reset_7d"] == last["reset_7d"]]
        first = same[0]
        span = last["epoch"] - first["epoch"]
        rate = (last["util_7d"] - first["util_7d"]) / span if span > 0 else 0.0
        out.append(
            {
                "label": label,
                "bearer": bid,
                "util": float(last["util_7d"]),
                "reset": float(last["reset_7d"]),
                "sampled_at": last["epoch"],
                "window_start_util": float(first["util_7d"]),
                "window_start_at": first["epoch"],
                "rate": max(0.0, rate),
            }
        )
    return out


def _fleet_excess(states: list[dict[str, Any]], util_by: dict[str, float]) -> float:
    excess = [util_by[s["label"]] - s["allowed_7d"] for s in states if s["allowed_7d"] is not None]
    return sum(excess) / len(excess) if excess else 0.0


class Window(NamedTuple):
    priority_share: float
    now0: float
    horizon_s: float


def simulate(
    mode: str,
    burst: float,
    k: float,
    demand: float,
    accounts: list[dict[str, Any]],
    win: Window,
) -> dict[str, Any]:
    """Fluid replay. off/observe: each account's demand stays on its own account.

    enforce: the decision from usage_allocator picks where non-priority demand may
    land (spill-only), or defers it. pi-prop: comparison column, proportional
    admission on fleet excess over the burst, spread over all accounts.
    """
    priority_share, now0, horizon_s = win
    accs = [dict(a) for a in accounts]
    labels = [a["label"] for a in accs]
    own = {a["label"]: demand * a["rate"] * STEP_S for a in accs}
    pending: dict[int, dict[str, float]] = {}
    closed: dict[str, float] = {}
    served = prio_units = prio_all_ahead = deferred_units = dropped_units = 0.0
    max_wait = 0.0
    episode = longest = 0

    for i in range(int(horizon_s // STEP_S)):
        now = now0 + i * STEP_S
        for a in accs:
            if now >= a["reset"] and a["label"] not in closed:
                closed[a["label"]] = a["util"]
                a["util"], a["reset"] = 0.0, a["reset"] + WEEK_S
        states = [ua.account_state(a["label"], a["util"], a["reset"], now, burst) for a in accs]
        util_by = {a["label"]: a["util"] for a in accs}
        reset_by = {a["label"]: a["reset"] for a in accs}
        all_ahead = all(s["state"] == "ahead" for s in states)

        arrivals = {lab: own[lab] for lab in labels}
        retry_units = pending.pop(i, {})
        retry_total = sum(retry_units.values())
        for lab, units in retry_units.items():
            arrivals[lab] = arrivals.get(lab, 0.0) + units
        fresh = sum(arrivals.values())
        prio = fresh * priority_share
        nonp = fresh - prio

        # Priority: the proxy never defers it. Spill-only route when some account is ahead.
        pv, plabels = ua.decide_pacing(
            states,
            mode="enforce" if mode != "off" else "off",
            priority=True,
            now=now,
            cap_s=RETRY_CAP_S,
            reset_by_label=reset_by,
            util_by_label=util_by,
            burst=burst,
        )
        prio_targets = plabels if (pv == "route" and plabels) else labels
        prio_units += prio
        prio_all_ahead += prio if all_ahead else 0.0

        if mode in ("off", "observe"):
            # No routing change: each account keeps its own demand (observe is read-only).
            refused, wait = 0.0, 0.0
            spread = dict(arrivals)
        elif mode == "enforce":
            dv, detail = ua.decide_pacing(
                states,
                mode="enforce",
                priority=False,
                now=now,
                cap_s=RETRY_CAP_S,
                reset_by_label=reset_by,
                util_by_label=util_by,
                burst=burst,
            )
            if dv == "defer":
                refused, wait = nonp, float(detail) * k
                targets = []
            else:
                refused, wait = 0.0, 0.0
                targets = detail if detail else labels
            spread = {lab: 0.0 for lab in labels}
            for lab in prio_targets:
                spread[lab] += prio / len(prio_targets)
            for lab in targets:
                spread[lab] += (nonp - refused) / len(targets) if targets else 0.0
        else:  # pi-prop comparison: proportional admit on fleet excess over burst
            excess = _fleet_excess(states, util_by)
            admit = min(1.0, max(0.0, 1.0 - max(0.0, excess) / burst))
            refused, wait = nonp * (1 - admit), PI_RETRY_S
            spread = {lab: (prio + nonp - refused) / len(labels) for lab in labels}

        for a in accs:
            add = spread.get(a["label"], 0.0)
            a["util"] += add
            served += add

        if refused > 0:
            # One client retry after Retry-After * k; a refusal on the retry is dropped.
            deferred_units += refused
            max_wait = max(max_wait, wait)
            episode += 1
            retry_share = (retry_total / (fresh or 1.0)) if fresh else 0.0
            fresh_refused = refused * (1 - min(1.0, retry_share))
            dropped_units += refused - fresh_refused
            if fresh_refused > 0:
                release = i + max(1, math.ceil(wait / STEP_S))
                bucket = pending.setdefault(release, {})
                for lab in labels:
                    bucket[lab] = bucket.get(lab, 0.0) + fresh_refused / len(labels)
        elif episode:
            longest = max(longest, episode)
            episode = 0
    longest = max(longest, episode)

    end_util = {}
    basis = {}
    for a in accs:
        if a["label"] in closed:
            end_util[a["label"]], basis[a["label"]] = closed[a["label"]], "window_reset"
        else:
            end_util[a["label"]], basis[a["label"]] = a["util"], "horizon_end"
    backlog = sum(sum(b.values()) for b in pending.values())
    return {
        "mode": mode,
        "burst": round(burst, 6),
        "k": k if mode == "enforce" else None,
        "demand": demand,
        "end_pace": {lab: round(v, 4) for lab, v in end_util.items()},
        "end_basis": basis,
        "max_end_pace": round(max(end_util.values()), 4),
        "deferred_request_events": round(deferred_units / UNIT_PER_TURN, 1),
        "max_deferral_s": round(max_wait, 1),
        "longest_deferral_episode_min": longest,
        "priority_requests": round(prio_units / UNIT_PER_TURN, 1),
        "priority_while_all_ahead_pct": round(100 * prio_all_ahead / prio_units, 2)
        if prio_units
        else 0.0,
        "dropped_after_retry_requests": round(dropped_units / UNIT_PER_TURN, 1),
        "backlog_at_horizon_requests": round(backlog / UNIT_PER_TURN, 1),
    }


def build_accounts(
    args_accounts: dict[str, str], meter: Path, as_of: float
) -> list[dict[str, Any]]:
    return read_meter(meter, as_of, args_accounts)


def run_grid(
    accounts: list[dict[str, Any]], priority_share: float, as_of: float
) -> list[dict[str, Any]]:
    horizon = max(a["reset"] for a in accounts) - as_of + 2 * STEP_S
    win = Window(priority_share, as_of, horizon)
    runs = []
    for d in DEMANDS:
        runs.append(simulate("off", 1 / 28, 1, d, accounts, win))
    for d in DEMANDS:
        for bname, b in BURSTS.items():
            runs.append({**simulate("observe", b, 1, d, accounts, win), "burst_label": bname})
    for d in DEMANDS:
        for bname, b in BURSTS.items():
            for k in KS:
                runs.append({**simulate("enforce", b, k, d, accounts, win), "burst_label": bname})
    for d in DEMANDS:
        for bname, b in BURSTS.items():
            runs.append({**simulate("pi-prop", b, 1, d, accounts, win), "burst_label": bname})
    for r in runs:
        r.setdefault("burst_label", "n/a" if r["mode"] == "off" else None)
    return runs


SCAN_DEMANDS = (0.80, 0.85, 0.90)


def demand_cap_scan(
    accounts: list[dict[str, Any]], priority_share: float, as_of: float
) -> list[dict[str, Any]]:
    """Enforce k=1 at lower demand: the cap at which each account ends <= 1.00x."""
    horizon = max(a["reset"] for a in accounts) - as_of + 2 * STEP_S
    win = Window(priority_share, as_of, horizon)
    rows = []
    for d in SCAN_DEMANDS:
        for bname, b in BURSTS.items():
            r = simulate("enforce", b, 1, d, accounts, win)
            rows.append({**r, "burst_label": bname})
    return rows


def recommend(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Least-deferral enforce config whose every account ends <= 1.00x."""
    ok = [r for r in runs if r["mode"] == "enforce" and r["max_end_pace"] <= 1.0]
    if ok:
        # Least deferral, then the highest demand that still fits, then the shipped
        # default burst (1/28) so a tie never changes config.
        best = min(
            ok,
            key=lambda r: (r["deferred_request_events"], -r["demand"], r["burst_label"] != "1/28"),
        )
        return {
            "meets_1x": True,
            **{
                k: best[k]
                for k in (
                    "burst_label",
                    "k",
                    "demand",
                    "max_end_pace",
                    "deferred_request_events",
                    "max_deferral_s",
                )
            },
        }
    best = min((r for r in runs if r["mode"] == "enforce"), key=lambda r: r["max_end_pace"])
    return {
        "meets_1x": False,
        "closest": {
            k: best[k]
            for k in ("burst_label", "k", "demand", "max_end_pace", "deferred_request_events")
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--scenario", choices=["today", "now"], default="today")
    p.add_argument("--meter-log", type=Path, default=DEFAULT_LOG)
    p.add_argument("--priority-share", type=float, default=DEFAULT_PRIORITY_SHARE)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.scenario == "today":
        as_of = TODAY_AS_OF.timestamp()
    else:
        as_of = dt.datetime.now(tz=BRT).timestamp()
    accounts = build_accounts(DEFAULT_ACCOUNTS, args.meter_log, as_of)
    for a in accounts:
        a["remaining_h"] = round((a["reset"] - as_of) / 3600, 2)
        a["rate_pct_per_h"] = round(100 * a["rate"] * 3600, 4)
    runs = run_grid(accounts, args.priority_share, as_of)
    scan = demand_cap_scan(accounts, args.priority_share, as_of)
    report = {
        "scenario": args.scenario,
        "as_of": dt.datetime.fromtimestamp(as_of, BRT).isoformat(),
        "accounts": accounts,
        "assumptions": {
            "priority_share": args.priority_share,
            "unit_per_turn": UNIT_PER_TURN,
            "retry_cap_s": RETRY_CAP_S,
            "step_s": STEP_S,
            "k": "sim-only multiplier on Retry-After (client over-wait)",
            "pi_prop": "comparison column only, not production code",
        },
        "runs": runs,
        "demand_cap_scan": scan,
        "recommendation": recommend(runs + scan),
    }
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for r in runs:
            print(r)
        print(report["recommendation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
