"""Credential probe for a proxy-owns-key lane.

#161 made a rejected key visible, but only once a request happened to hit the
lane. A dormant overflow lane (Kimi :8767) serves no requests for weeks, so it
kept reading healthy. The probe answers the question without waiting for
traffic, and without spending tokens.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from anthropic_throttle_proxy import config, proxy


@pytest.fixture
def owns_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AUTH_PROBE_MODEL", "kimi-k2.6")
    key = tmp_path / "key"
    key.write_text("sk-test-abc\n")
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "prefer")
    monkeypatch.setattr(config, "API_KEY_FILE", str(key))
    proxy._api_key_cache = None
    config.state["upstream_auth_ok"] = True
    config.state["upstream_auth_error"] = ""
    yield
    proxy._api_key_cache = None
    config.state["upstream_auth_ok"] = True
    config.state["upstream_auth_error"] = ""


async def _upstream(status: int, payload: dict) -> TestServer:
    async def messages(request: web.Request) -> web.Response:
        assert request.headers["authorization"] == "Bearer sk-test-abc"
        body = await request.json()
        assert body["max_tokens"] == 1  # a probe must never buy a completion
        assert body["model"] == "kimi-k2.6"
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_probe_closes_the_lane_on_a_rejected_key(owns_key, monkeypatch):
    server = await _upstream(401, {"error": {"message": "Incorrect API key provided"}})
    monkeypatch.setattr(config, "UPSTREAM", str(server.make_url("")).rstrip("/"))
    try:
        await proxy._probe_upstream_auth_once()
    finally:
        await server.close()
    assert config.state["upstream_auth_ok"] is False
    assert "Incorrect API key" in config.state["upstream_auth_error"]


async def test_probe_reopens_the_lane_on_success(owns_key, monkeypatch):
    config.state["upstream_auth_ok"] = False
    config.state["upstream_auth_error"] = "stale"
    server = await _upstream(200, {"type": "message", "content": []})
    monkeypatch.setattr(config, "UPSTREAM", str(server.make_url("")).rstrip("/"))
    try:
        await proxy._probe_upstream_auth_once()
    finally:
        await server.close()
    assert config.state["upstream_auth_ok"] is True
    assert config.state["upstream_auth_error"] == ""


@pytest.mark.parametrize("status", [404, 429, 500])
async def test_inconclusive_status_leaves_the_verdict_alone(owns_key, monkeypatch, status):
    """An upstream without /v1/models must not be read as a dead key."""
    server = await _upstream(status, {"error": {"message": "nope"}})
    monkeypatch.setattr(config, "UPSTREAM", str(server.make_url("")).rstrip("/"))
    try:
        await proxy._probe_upstream_auth_once()
    finally:
        await server.close()
    assert config.state["upstream_auth_ok"] is True


async def test_two_hundred_on_a_wrong_route_does_not_reopen(owns_key, monkeypatch):
    """`api.moonshot.ai/v1/models` answers 200 with a `url.not_found` body."""
    config.state["upstream_auth_ok"] = False
    config.state["upstream_auth_error"] = "Incorrect API key provided"
    server = await _upstream(200, {"code": 5, "error": "url.not_found", "status": False})
    monkeypatch.setattr(config, "UPSTREAM", str(server.make_url("")).rstrip("/"))
    try:
        await proxy._probe_upstream_auth_once()
    finally:
        await server.close()
    assert config.state["upstream_auth_ok"] is False


async def test_probe_without_a_model_is_disabled(owns_key, monkeypatch):
    """No model id = no way to ask; emit nothing rather than a 400-shaped lie."""
    monkeypatch.setattr(config, "AUTH_PROBE_MODEL", "")
    monkeypatch.setattr(config, "UPSTREAM", "http://127.0.0.1:1")
    await proxy._probe_upstream_auth_once()
    assert config.state["upstream_auth_ok"] is True


async def test_probe_is_a_no_op_without_an_own_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "API_KEY_FILE", "")
    proxy._api_key_cache = None
    # No upstream is started: reaching the network here would itself be the bug.
    monkeypatch.setattr(config, "UPSTREAM", "http://127.0.0.1:1")
    await proxy._probe_upstream_auth_once()
    assert config.state["upstream_auth_ok"] is True
