# Promoted from the Token Diet Notes prototype; canonical executable source is here.
#!/usr/bin/env python3
"""Project/week costs including all attempts. External task acceptance != artifact proxy."""

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

BRT = dt.timezone(dt.timedelta(hours=-3))


def week(epoch):
    d = dt.datetime.fromtimestamp(epoch, BRT).date()
    return (d - dt.timedelta(days=d.weekday())).strftime("%d/%m/%Y")


def build(attempts, outcomes, freeze_epoch, basis="calendar", coverage_complete=False):
    if basis not in {"calendar", "task_cohort"}:
        raise ValueError("unknown week basis")
    seen = set()
    tasks = {}
    groups = collections.defaultdict(list)
    resources = collections.defaultdict(set)
    for a in attempts:
        if a["attempt_key"] in seen:
            raise ValueError("duplicate attempt")
        seen.add(a["attempt_key"])
        if a["attempt_epoch"] < a["task_start_epoch"]:
            raise ValueError("attempt precedes task")
        if a["points"] is not None and (
            isinstance(a["points"], bool) or not math.isfinite(a["points"]) or a["points"] < 0
        ):
            raise ValueError("invalid points")
        if a["tokens"] is not None and (
            isinstance(a["tokens"], bool) or not isinstance(a["tokens"], int) or a["tokens"] < 0
        ):
            raise ValueError("invalid tokens")
        label = (
            "future_unseen"
            if a["task_start_epoch"] >= freeze_epoch
            else "exploratory_holdout"
            if a["session_hash"][-1:] in {"0", "5"}
            else "exploratory_fit"
        )
        identity = (a["project_key"], a["session_hash"], a["arm"], a["task_start_epoch"], label)
        if a["task_key"] in tasks and tasks[a["task_key"]] != identity:
            raise ValueError("task assignment drift")
        tasks[a["task_key"]] = identity
        project, _, arm, _, label = identity
        unit, cal = a["points_unit"], a["calibration_id"]
        w = week(a["attempt_epoch"] if basis == "calendar" else a["task_start_epoch"])
        groups[(project, w, label, arm, unit, cal)].append(a)
        resources[(project, label, arm)].add((unit, cal))
    final = {}
    proxies = {}
    seen_outcomes = set()
    for o in outcomes:
        task = o["task_key"]
        if task not in tasks:
            raise ValueError("outcome without enrolled task")
        if o["grade"] == "accepted_task":
            if task in seen_outcomes:
                raise ValueError("multiple final task verdicts")
            seen_outcomes.add(task)
            if o.get("oracle_kind") not in {"declared_oracle", "human"} or not o.get(
                "oracle_provenance_verified"
            ):
                raise ValueError("unverified/self acceptance")
            if not isinstance(o["accepted"], bool):
                raise ValueError("unknown success cannot be recoded")
            final[task] = o
        elif o["grade"] == "artifact_proxy":
            if o.get("dedup_complete") and o.get("work_unit_key"):
                proxies.setdefault(o["work_unit_key"], []).append(o)
        elif o["grade"] != "unknown":
            raise ValueError("unknown outcome grade")
    denoms = collections.defaultdict(list)
    proxy_denoms = collections.defaultdict(set)
    for task, o in final.items():
        project, _, arm, start, label = tasks[task]
        w = week(o["accepted_epoch"] if basis == "calendar" else start)
        denoms[(project, w, label, arm)].append(o)
    for proxy, oo in proxies.items():
        identities = {tasks[o["task_key"]] for o in oo}
        if len(identities) != 1:
            continue  # ambiguity is not assigned by cost shares
        o = oo[0]
        project, _, arm, start, label = next(iter(identities))
        w = week(o["accepted_epoch"] if basis == "calendar" else start)
        proxy_denoms[(project, w, label, arm)].add(proxy)
    # Include acceptance-only weeks: accepting last week's work must not disappear.
    for project, w, label, arm in set(denoms) | set(proxy_denoms):
        for unit, cal in resources[(project, label, arm)]:
            groups.setdefault((project, w, label, arm, unit, cal), [])
    rows = []
    for key, aa in sorted(groups.items()):
        project, w, label, arm, unit, cal = key
        roots = {a["task_key"] for a in aa}
        events = denoms[(project, w, label, arm)]
        successes = sum(o["accepted"] for o in events)
        failures = sum(not o["accepted"] for o in events)
        known_roots = {a["task_key"] for a in aa if a.get("root_task_known", True)}
        unknown = sum(t not in final for t in known_roots)
        known_points = sum(a["points"] or 0 for a in aa)
        known_tokens = sum(a["tokens"] or 0 for a in aa)
        missing_points = sum(a["points"] is None for a in aa)
        missing_tokens = sum(a["tokens"] is None for a in aa)
        complete = coverage_complete and not unknown and len(known_roots) == len(roots)
        rows.append(
            dict(
                project_key=project,
                week_brt=w,
                week_basis=basis,
                coverage_complete=complete,
                held_out=label != "exploratory_fit",
                holdout_kind=label,
                arm=arm,
                points_unit=unit,
                calibration_id=cal,
                attempts=len(aa),
                tasks_with_spend=len(known_roots),
                unattributed_requests=sum(not a.get("root_task_known", True) for a in aa),
                failed_attempts=sum(a["status"] == "failed" for a in aa),
                rework_attempts=sum(a["status"] == "rework" for a in aa),
                accepted_tasks_observed=successes,
                known_failed_task_verdicts=failures,
                unknown_success_tasks=unknown,
                artifact_proxies=len(proxy_denoms[(project, w, label, arm)]),
                total_points=known_points if not missing_points else None,
                known_points_subtotal=known_points,
                missing_points_attempts=missing_points,
                total_tokens=known_tokens if not missing_tokens else None,
                known_tokens_subtotal=known_tokens,
                missing_tokens_attempts=missing_tokens,
                points_per_accepted_task=known_points / successes
                if complete and successes and not missing_points
                else None,
                tokens_per_accepted_task=known_tokens / successes
                if complete and successes and not missing_tokens
                else None,
                confirmatory_ready=False,
                claim="A_observational_until_registered_randomized_unseen_trial",
            )
        )
    return rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    x = json.loads(a.input.read_text())
    rows = build(
        x["attempts"],
        x["outcomes"],
        x["freeze_epoch"],
        coverage_complete=x.get("coverage_complete") is True,
    )
    cohorts = build(
        x["attempts"],
        x["outcomes"],
        x["freeze_epoch"],
        "task_cohort",
        coverage_complete=x.get("coverage_complete") is True,
    )
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(
        json.dumps(
            dict(
                schema=1,
                calendar_rows=rows,
                task_cohort_rows=cohorts,
                source_sha256=hashlib.sha256(a.input.read_bytes()).hexdigest(),
                contract=(
                    "normalized metadata only; complete attempts required, unknown remains unknown"
                ),
            ),
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    print(json.dumps(dict(project_week_rows=len(rows), task_cohort_rows=len(cohorts))))


if __name__ == "__main__":
    main()
