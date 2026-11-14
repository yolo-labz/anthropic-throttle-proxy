"""Executable deployment acceptance. Synthetic CPU work is not a savings experiment."""

import json
import os

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
    base = os.environ.get("SCIENCE_NOTEBOOK_URL", "http://localhost:5000")
    endpoint = base + "/auth/token"
    notebook = requests.get(endpoint, timeout=15)
    if notebook.status_code != 200 or notebook.json() != {"token": None}:
        raise RuntimeError(f"notebook still requires an application login: {notebook.status_code}")
    tracking = os.environ["MLFLOW_TRACKING_URI"]
    experiments = requests.post(
        tracking + "/api/2.0/mlflow/experiments/search", json={}, timeout=15
    )
    if experiments.status_code != 200 or "experiments" not in experiments.json():
        raise RuntimeError(
            f"tracking still requires an application login: {experiments.status_code}"
        )
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
            {"application_login": "disabled; Tailscale ingress only", "desktop_cpu": result},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
