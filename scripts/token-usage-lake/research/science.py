"""Notebook helpers. Dataset snapshots and desktop compute are explicit choices."""

import json
import os
from pathlib import Path


def latest_snapshot(root=None, run_id=None):
    root = Path(root or os.environ.get("SCIENCE_SNAPSHOTS", "/workspace/data/snapshots"))
    run_id = run_id or os.environ.get("SCIENCE_SNAPSHOT_RUN_ID")
    if run_id and any(c not in "0123456789abcdef-" for c in run_id):
        raise ValueError("invalid snapshot identity")
    path = root / run_id / "manifest.json" if run_id else root / "latest.json"
    manifest = json.loads(path.read_text())
    if run_id and manifest["run_id"] != run_id:
        raise ValueError("snapshot identity mismatch")
    return root / manifest["run_id"], manifest


def desktop_client():
    from distributed import Client, Security

    root = Path(os.environ.get("SCIENCE_TLS", "/run/science-tls"))
    security = Security(
        tls_ca_file=str(root / "ca.crt"),
        tls_client_cert=str(root / "client.crt"),
        tls_client_key=str(root / "client.key"),
        require_encryption=True,
    )
    return Client(os.environ["SCIENCE_SCHEDULER"], security=security, timeout="15s")


def configure_tracking():
    import mlflow

    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    return mlflow
