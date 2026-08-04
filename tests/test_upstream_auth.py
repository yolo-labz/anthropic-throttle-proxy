"""Lane-level credential verdict.

04/08/2026: the Kimi lane (:8767, proxy-owns-key) rendered `HEALTHY egress ok`
with cap 6 on the dashboard while every request to it returned
``{"error":{"message":"Incorrect API key provided"}}``. Reachability and DNS
say nothing about whether a lane's own key still works, and `lane_usable` then
held the lane OPEN for spill because a proxy-owns-key lane legitimately tracks
zero bearers.
"""

from __future__ import annotations

import pytest

from anthropic_throttle_proxy import config, forwarding, routing
from anthropic_throttle_proxy.ui import routes

REJECTED = b'{"error":{"message":"Incorrect API key provided","type":"incorrect_api_key_error"}}'


@pytest.fixture
def owns_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "prefer")
    monkeypatch.setattr(config, "API_KEY_FILE", "/run/secrets/moonshot")
    config.state["upstream_auth_ok"] = True
    config.state["upstream_auth_error"] = ""
    yield
    config.state["upstream_auth_ok"] = True
    config.state["upstream_auth_error"] = ""


def test_rejected_key_closes_the_lane_and_keeps_the_reason(owns_key):
    forwarding.note_upstream_auth(401, REJECTED)
    assert config.state["upstream_auth_ok"] is False
    assert "Incorrect API key" in config.state["upstream_auth_error"]


def test_success_reopens_without_a_restart(owns_key):
    forwarding.note_upstream_auth(403, REJECTED)
    assert config.state["upstream_auth_ok"] is False
    forwarding.note_upstream_auth(200)
    assert config.state["upstream_auth_ok"] is True
    assert config.state["upstream_auth_error"] == ""


def test_throttle_and_server_errors_are_not_auth_verdicts(owns_key):
    for status in (429, 500, 503, 529):
        forwarding.note_upstream_auth(status, b"{}")
    assert config.state["upstream_auth_ok"] is True


def test_client_provides_key_lane_never_publishes_a_lane_verdict(monkeypatch):
    """A 401 on the Anthropic lane is ONE client's stale OAuth token.

    Publishing that as a lane-wide verdict would close a lane that is serving
    every other bearer fine.
    """
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "API_KEY_FILE", "")
    config.state["upstream_auth_ok"] = True
    forwarding.note_upstream_auth(401, REJECTED)
    assert config.state["upstream_auth_ok"] is True


def test_lane_usable_closes_on_rejected_key():
    health = {"upstream_egress_ok": True, "bearers": {}, "upstream_auth_ok": False}
    assert routing.lane_usable(health, proxy_owns_key=True) == (False, "upstream-auth-rejected")


def test_lane_usable_unchanged_when_auth_is_unknown_or_ok():
    # None = the sibling does not own a key; absent = an older proxy build.
    for value in (None, True):
        health = {"upstream_egress_ok": True, "bearers": {}, "upstream_auth_ok": value}
        assert routing.lane_usable(health, proxy_owns_key=True) == (
            True,
            "no-bearers-proxy-owns-key",
        )
    legacy = {"upstream_egress_ok": True, "bearers": {}}
    assert routing.lane_usable(legacy, proxy_owns_key=True) == (True, "no-bearers-proxy-owns-key")


def _fleet_row(**over):
    row = {
        "name": "kimi",
        "ok": True,
        "upstream": "https://api.moonshot.ai/anthropic",
        "upstream_egress_ok": True,
        "inflight": 0,
        "queued": 0,
        "served": 0,
        "max_concurrent": 6,
        "err": "",
    }
    row.update(over)
    return row


def _sibling(**over):
    return routes._build_providers(
        upstream="https://api.anthropic.com",
        central_url="(direct)",
        central_status="unknown",
        level="healthy",
        inflight=0,
        queued=0,
        served=0,
        max_concurrent=5,
        fleet=[_fleet_row(**over)],
    )[1]


def test_dashboard_marks_a_dead_key_crit_not_healthy():
    row = _sibling(upstream_auth_ok=False, upstream_auth_error="Incorrect API key provided")
    assert row["level"] == "crit"
    assert row["auth_dead"] is True
    assert row["err"] == "Incorrect API key provided"


def test_dashboard_leaves_a_client_key_sibling_healthy():
    row = _sibling(upstream_auth_ok=None)
    assert row["level"] == "healthy"
    assert row["auth_dead"] is False
