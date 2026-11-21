"""Executable deployment acceptance. Synthetic CPU work is not a savings experiment."""

import json
import os
from pathlib import Path

import requests
from science import configure_tracking, desktop_client


def cpu_interval(values):
    import socket

    import numpy as np

    sample = np.asarray(values, dtype=float)
    draws = np.random.default_rng(20261114).choice(sample, size=(1000, len(sample)))
    lo, hi = np.quantile(draws.mean(axis=1), [0.025, 0.975])
    return {
        "worker_host": socket.gethostname(),
        "mean": float(sample.mean()),
        "lower": float(lo),
        "upper": float(hi),
    }


def main():
    root = Path(os.environ.get("SCIENCE_AUTH", "/run/science-auth"))
    base = os.environ.get("SCIENCE_NOTEBOOK_URL", "http://localhost:5000")
    endpoint = base + "/auth/token"
    rejected = requests.get(endpoint, timeout=15)
    if rejected.status_code not in (401, 403):
        raise RuntimeError(f"notebook API did not reject anonymous access: {rejected.status_code}")
    token = (root / "notebook-token").read_text().strip()
    accepted = requests.get(endpoint, headers={"Authorization": "Bearer " + token}, timeout=15)
    if accepted.status_code != 200 or accepted.json().get("token") != token:
        raise RuntimeError(f"notebook authentication failed: {accepted.status_code}")
    tracking = os.environ["MLFLOW_TRACKING_URI"]
    rejected = requests.post(tracking + "/api/2.0/mlflow/experiments/search", json={}, timeout=15)
    if rejected.status_code != 401:
        raise RuntimeError(f"tracking API did not reject anonymous access: {rejected.status_code}")
    with desktop_client() as client:
        result = client.submit(cpu_interval, list(range(100)), pure=False).result(timeout=45)
    if result["worker_host"] != "desktop" or not result["lower"] < result["mean"] < result["upper"]:
        raise RuntimeError("desktop numerical computation failed")
    mlflow = configure_tracking()
    mlflow.set_experiment("Token Diet infrastructure checks")
    with mlflow.start_run(run_name="desktop-cpu-connectivity") as run:
        mlflow.set_tags(
            {
                "purpose": "infrastructure_validation",
                "data": "synthetic",
                "task_success_measured": "false",
                "savings_measured": "false",
            }
        )
        mlflow.log_param("worker_host", result["worker_host"])
        mlflow.log_metrics({k: result[k] for k in ("mean", "lower", "upper")})
        result["mlflow_run_id"] = run.info.run_id
    print(
        json.dumps(
            {"anonymous_access": "rejected", "notebook_auth": "passed", "desktop_cpu": result},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
