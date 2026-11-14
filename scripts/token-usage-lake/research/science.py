"""Notebook helpers. Dataset snapshots and desktop compute are explicit choices."""

import json
import os
from pathlib import Path


def latest_snapshot(root=None):
    root = Path(root or os.environ.get("SCIENCE_SNAPSHOTS", "/workspace/data/snapshots"))
    manifest = json.loads((root / "latest.json").read_text())
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
    import configparser

    import mlflow

    config = configparser.ConfigParser()
    root = Path(os.environ.get("SCIENCE_AUTH", "/run/science-auth"))
    config.read(root / "mlflow-auth.ini")
    os.environ["MLFLOW_TRACKING_USERNAME"] = config["mlflow"]["admin_username"]
    os.environ["MLFLOW_TRACKING_PASSWORD"] = config["mlflow"]["admin_password"]
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    return mlflow
