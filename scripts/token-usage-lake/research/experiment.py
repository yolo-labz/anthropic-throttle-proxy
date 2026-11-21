"""One offline path: pinned dataset -> all-attempt outcomes -> paired exploratory analysis."""

import argparse
import hashlib
import json
import platform
from collections import defaultdict
from pathlib import Path

from outcomes import build
from science import latest_snapshot
from trial_stats import paired_cluster_bootstrap, quality_gate

_RESET_SOURCE_PINS = {
    "reset_contract_adapter.py": "369abd39d2f0df15a3fbb289b15de7a626f5acd330513d40a5bd8d85c258db8a",
    "reset_readiness_v3.py": "50f8290e62247cfb645d1917e736481598c8bd3ce25582b558d952b96c0e544e",
    "reset-observation-contract-v2.json": (
        "5cc8b9a4b4b622c4443bc6d14cd5bffc754a891052cc9e70e39ff984994dde38"
    ),
}


def _verified_reset_sources():
    """Check every opt-in invocation, even when Python has cached the adapter import."""
    verified = {}
    for name, expected in _RESET_SOURCE_PINS.items():
        actual = hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("reset_workflow_source_pin_mismatch:" + name)
        verified[name] = actual
    return verified


def _reset_metadata(parameters):
    """Evaluate explicit reset metadata without changing retrospective analysis gates."""
    required = {
        "now",
        "observations",
        "required_scopes",
        "max_source_age_s",
        "max_poll_age_s",
        "max_clock_skew_s",
    }
    if type(parameters) is not dict or set(parameters) != required:
        raise ValueError("reset_metadata requires exactly the six adapter parameters")
    _verified_reset_sources()
    # Import only for the opt-in path, after rechecking all source pins on disk.
    from reset_contract_adapter import evaluate_observations

    return evaluate_observations(**parameters)


def analyze(data):
    """Reuse the existing outcome and session-bootstrap tools; never certify a saving."""
    if data.get("usage_grain") not in {"native", "gateway"}:
        raise ValueError("declare one usage grain; native and gateway spend cannot be added")
    attempts, outcomes = data["attempts"], data["outcomes"]
    if any(a["arm"] not in {"control", "treatment"} for a in attempts):
        raise ValueError("expected control/treatment assignments")
    rows = build(
        attempts,
        outcomes,
        data["freeze_epoch"],
        "task_cohort",
        coverage_complete=data.get("coverage_complete") is True,
    )
    blockers = []
    if not rows or not all(row["coverage_complete"] for row in rows):
        blockers.append("incomplete attempt coverage or unknown task acceptance")
    metric = data.get("metric", "tokens")
    if metric not in {"tokens", "points"}:
        raise ValueError("metric must be tokens or points")
    if any(row["total_" + metric] is None for row in rows):
        blockers.append("missing attempt costs")
    strata = {(a["project_key"], a["points_unit"], a["calibration_id"]) for a in attempts}
    if len(strata) != 1:
        blockers.append("analyze one project/resource/calibration stratum at a time")
    if data.get("assignment_unit") != "paired_session":
        blockers.append("paired independent session assignment is not declared")

    report = {
        "claim": "exploratory; not a causal or quota-saving result",
        "realized_savings": None,
        "usage_grain": data["usage_grain"],
        "metric": metric,
        "cohort_rows": rows,
        "blockers": blockers,
        "effect": None,
        "quality": None,
        "analysis_ready": False,
    }
    if "reset_metadata" in data:
        report["reset_metadata"] = _reset_metadata(data["reset_metadata"])
    if blockers:
        return report
    verdicts = {o["task_key"]: o["accepted"] for o in outcomes if o["grade"] == "accepted_task"}
    sessions = defaultdict(lambda: defaultdict(lambda: {"cost": 0, "tasks": set()}))
    for attempt in attempts:
        arm = sessions[attempt["session_hash"]][attempt["arm"]]
        arm["cost"] += attempt[metric]  # Includes every failed, aborted and rework attempt.
        arm["tasks"].add(attempt["task_key"])
    if len(sessions) < 2 or any(
        set(arms) != {"control", "treatment"} for arms in sessions.values()
    ):
        blockers.append("need at least two complete paired session clusters; no rows dropped")
        return report
    clusters = []
    totals = {arm: [0, 0] for arm in ("control", "treatment")}
    for session in sorted(sessions):
        cluster = {}
        for arm, values in sessions[session].items():
            successes = sum(verdicts[task] for task in values["tasks"])
            cluster[arm + "_accepted"] = successes
            cluster[arm + "_" + metric] = values["cost"]
            totals[arm][0] += successes
            totals[arm][1] += len(values["tasks"])
        clusters.append(cluster)
    if any(successes == 0 for successes, _ in totals.values()):
        blockers.append("no accepted-task denominator in an arm")
        return report
    report["effect"] = paired_cluster_bootstrap(clusters, metric=metric)
    report["quality"] = quality_gate(totals["treatment"], totals["control"])
    if report["effect"]["interval90"] is None:
        blockers.append("too many bootstrap draws lack an accepted-task denominator")
    report["analysis_ready"] = not blockers
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="normalized metadata JSON")
    parser.add_argument("--snapshots", type=Path, required=True, help="published snapshot root")
    parser.add_argument("--out", type=Path, required=True, help="new report; never overwritten")
    args = parser.parse_args(argv)
    try:
        raw = args.input.read_bytes()
        data = json.loads(raw)
        run_id = data["snapshot_run_id"]
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("an explicit snapshot_run_id is required")
        snapshot, manifest = latest_snapshot(args.snapshots, run_id)
        manifest_hash = hashlib.sha256((snapshot / "manifest.json").read_bytes()).hexdigest()
        if data["snapshot_manifest_sha256"] != manifest_hash:
            raise ValueError("normalized facts refer to a different snapshot manifest")
        report = analyze(data)
        report["provenance"] = {
            "run_id": run_id,
            "manifest_sha256": manifest_hash,
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "analysis_sha256": {
                name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ("experiment.py", "outcomes.py", "trial_stats.py", "science.py")
            },
            "python": platform.python_version(),
            "source_quality": manifest.get("data_quality"),
            "boundary": "dataset binding is not independent proof of normalized outcome provenance",
        }
        if "reset_metadata" in data:
            # Refuse drift during analysis and record only verified pinned hashes.
            report["provenance"]["analysis_sha256"].update(_verified_reset_sources())
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "analysis_ready": report["analysis_ready"],
                "realized_savings": None,
                "blockers": report["blockers"],
            }
        )
    )
    return 0 if report["analysis_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
