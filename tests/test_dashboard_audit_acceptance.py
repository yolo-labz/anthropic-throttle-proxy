"""Coordinator-owned, synthetic acceptance checks frozen before swarm output.

These certify specific source/data defects, NOT live Firefox rendering, current
provider policy, deployment, or completion of all 17 audit findings.
"""

import json

import pytest

from anthropic_throttle_proxy import fleet_ui_config, lanes
from anthropic_throttle_proxy.ui import routes


def test_audit_configuration_cannot_rewrite_observed_plan():
    source = {"id": "codex:c", "plan": "observed-tier", "status": "ok"}
    cfg = {"subscriptions": [{"id": "codex:c", "label": "Account C", "plan": "caption"}]}
    row = fleet_ui_config.decorate([source], cfg)["rows"][0]
    assert row["plan"] == "observed-tier"
    assert row["plan_caption"] == "caption"
    assert row["plan_conflict"] is True
    assert source["plan"] == "observed-tier"


def test_audit_configuration_without_observation_is_not_an_observed_plan():
    cfg = {"subscriptions": [{"id": "codex:c", "label": "Account C", "plan": "caption"}]}
    row = fleet_ui_config.decorate([], cfg)["rows"][0]
    assert not row["plan"]
    assert row["plan_caption"] == "caption"
    assert row["status"] != "ok"


@pytest.mark.parametrize(
    ("endpoint", "identity"),
    [("http://127.0.0.1:8766", "127.0.0.1"), ("http://[::1]:8766", "::1")],
)
def test_audit_endpoint_identity_is_not_truncated(endpoint, identity):
    assert routes._provider_label(endpoint) == identity


def test_audit_running_ui_does_not_prove_dns_or_egress():
    provider = routes._build_providers(
        upstream="http://127.0.0.1:1",
        central_url="(direct)",
        central_status="unknown",
        level="idle",
        inflight=0,
        queued=0,
        served=0,
        max_concurrent=2,
        fleet=[],
    )[0]
    assert provider["ok"] is True  # this process really can render the UI
    assert provider["egress_ok"] is None  # no DNS observation was supplied


@pytest.mark.parametrize("stamp", [None, "not-a-date", "2099-01-01T00:00:00Z"])
def test_audit_untrusted_observation_time_cannot_grant_fresh_capacity(stamp, tmp_path, monkeypatch):
    path = tmp_path / "synthetic-lanes.json"
    path.write_text(
        json.dumps(
            {
                "generatedAt": stamp,
                "intervalSeconds": 900,
                "lanes": [{"id": "codex:c", "kind": "codex", "status": "ok"}],
            }
        )
    )
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(path))
    lanes._cache = None
    view = lanes.view(1_760_000_000.0)
    assert all(row["status"] != "ok" for row in view["lanes"])


def test_audit_refused_unpaced_bearer_never_claims_healthy_capacity():
    bearer = {"bearer_id": "synthetic", "unified": {}, "queued": 0, "limiter": None}
    status = routes._compute_status(
        [bearer],
        "fair",
        1_760_000_000.0,
        credential_verdicts={"synthetic": {"ok": False, "detail": "refused"}},
    )
    assert status["level"] != "healthy"
    assert "HEALTHY" not in status["verdict"]
