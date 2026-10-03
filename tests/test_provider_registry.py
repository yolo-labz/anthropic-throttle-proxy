"""THRTL-19: synthetic-only policy reload and active-stream preservation."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import ingress, provider_registry, routing
from anthropic_throttle_proxy.routing import Lane, LaneState


def _policy(path, lanes):
    path.write_text(json.dumps({"version": 1, "lanes": lanes}))


@pytest.fixture
def registry(monkeypatch, tmp_path):
    path = tmp_path / "registry.json"
    monkeypatch.setattr(ingress, "PROVIDER_REGISTRY_PATH", str(path))
    monkeypatch.setattr(ingress, "_registry_lanes", frozenset())
    monkeypatch.setattr(ingress, "_registry_error", "not-loaded")
    monkeypatch.setattr(ingress, "_session_lane", {})
    monkeypatch.setattr(ingress, "LANES", routing.default_lanes())
    monkeypatch.setattr(ingress, "lane_state", {})
    monkeypatch.setattr(ingress, "LANE_HEALTH_INTERVAL_S", 0)
    monkeypatch.setattr(routing, "GENERATE_OVERFLOW_ENABLED", False)
    return path


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b"[]",
        b'{"version":true,"lanes":[]}',
        b'{"version":2,"lanes":[]}',
        b'{"version":1,"lanes":"anthropic"}',
        b'{"version":1,"lanes":[{}]}',
        b'{"version":1,"lanes":["not-configured"]}',
        b'{"version":1,"lanes":["anthropic","anthropic"]}',
        b'{"version":1,"lanes":[],"models":{}}',
        b'{"version":1,"lanes":[],"lanes":["anthropic"]}',
        b'{"version":2,"version":1,"lanes":[]}',
        b" " * (provider_registry.MAX_BYTES + 1),
    ],
)
async def test_invalid_reload_closed_then_last_good(registry, raw):
    registry.write_bytes(raw)
    await ingress._reload_provider_registry()
    assert ingress._registry_lanes == frozenset()
    assert ingress._registry_error == "registry-unreadable-or-invalid"
    _policy(registry, ["anthropic"])
    await ingress._reload_provider_registry()
    previous = ingress._registry_lanes
    registry.write_bytes(raw)
    await ingress._reload_provider_registry()
    assert ingress._registry_lanes is previous
    assert ingress._registry_error == "registry-unreadable-or-invalid"


async def test_missing_is_not_disable_and_empty_is_explicit_deny(registry):
    await ingress._reload_provider_registry()
    assert ingress._registry_lanes == frozenset()
    _policy(registry, ["anthropic"])
    await ingress._reload_provider_registry()
    registry.unlink()
    await ingress._reload_provider_registry()
    assert ingress._registry_lanes == frozenset({"anthropic"})
    assert ingress._registry_error
    _policy(registry, [])
    await ingress._reload_provider_registry()
    assert ingress._registry_lanes == frozenset()
    assert ingress._registry_error is None


async def test_fifo_rejected_without_blocking_health_poll(registry):
    os.mkfifo(registry)
    await ingress._reload_provider_registry()
    assert ingress._registry_lanes == frozenset()
    assert ingress._registry_error == "registry-unreadable-or-invalid"


async def test_default_off_does_not_read_or_change_selection(monkeypatch):
    monkeypatch.setattr(ingress, "PROVIDER_REGISTRY_PATH", "")
    monkeypatch.setattr(ingress, "_registry_lanes", None)

    def forbidden(*_args):
        pytest.fail("disabled registry must not read a file")

    monkeypatch.setattr(provider_registry, "load", forbidden)
    await ingress._reload_provider_registry()
    for overflow in (False, True):
        monkeypatch.setattr(routing, "GENERATE_OVERFLOW_ENABLED", overflow)
        for role in routing.ROLES:
            assert ingress._effective_chain(role) == routing.effective_chain(role)
    health = json.loads((await ingress._health(None)).body)
    assert "provider_registry" not in health


async def test_registry_cannot_widen_role_overflow_or_capacity(registry, monkeypatch):
    _policy(registry, list(ingress.LANES))
    await ingress._reload_provider_registry()
    assert ingress._select_open_lane("generate") is None  # no capacity evidence
    monkeypatch.setattr(
        ingress, "lane_state", {lid: LaneState(True, time.time()) for lid in ingress.LANES}
    )
    assert ingress._effective_chain("generate") == ("anthropic",)
    assert "anthropic" not in ingress._effective_chain("bulk")
    ingress._session_lane["synthetic"] = "anthropic"
    assert ingress._pinned_lane_choice("bulk", "synthetic", set()) is None
    _policy(registry, ["codex"])
    await ingress._reload_provider_registry()
    assert ingress._select_open_lane("generate") is None
    assert ingress._select_open_lane("code") == "codex"
    assert not ingress._has_spill_target("generate", set(), None)
    assert not ingress._has_spill_target("generate", set(), "subscription")
    assert ingress._session_lane == {"synthetic": "anthropic"}


@pytest.mark.parametrize(
    "mode,capacity,revoke",
    [("unknown", True, False), ("subscription", False, False), ("subscription", True, True)],
)
async def test_membership_requires_fresh_eligible_policy(
    registry, monkeypatch, mode, capacity, revoke
):
    _policy(registry, ["anthropic"])
    await ingress._reload_provider_registry()

    async def probe(_session, lane):
        ingress.lane_state[lane.id] = LaneState(
            True, time.time(), credential_mode=mode, credential_capacity_ok=capacity
        )
        if revoke:
            _policy(registry, [])
            await ingress._reload_provider_registry()

    monkeypatch.setattr(ingress, "_probe_lane_health", probe)
    assert await ingress._select_constrained_lane(None, "generate", set()) is None
    if revoke:
        refusal = ingress._policy_refusal("generate", "subscription")
        assert refusal.status == 403
        assert json.loads(refusal.body)["error"]["eligible_configured"] == 0


async def test_reload_preserves_active_stream_lane_and_session(registry, monkeypatch):
    release = asyncio.Event()
    hits = []
    first = b'data: {"part":1}\n\n'
    last = b'data: {"part":2}\n\n'

    async def stream(request):
        hits.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(first)
        await release.wait()
        await response.write(last)
        await response.write_eof()
        return response

    lane_app = web.Application()
    lane_app.router.add_post("/v1/messages", stream)
    async with TestServer(lane_app) as upstream:
        lane = Lane("anthropic", str(upstream.make_url("")).rstrip("/"), frozenset({"generate"}))
        monkeypatch.setattr(ingress, "LANES", {"anthropic": lane})
        monkeypatch.setattr(ingress, "lane_state", {"anthropic": LaneState(True, time.time())})
        _policy(registry, ["anthropic"])
        async with TestClient(TestServer(ingress.build_app())) as client:
            try:
                async with asyncio.timeout(5):
                    body = {
                        "model": "synthetic-model",
                        "metadata": {"user_id": "synthetic-session"},
                        "messages": [{"role": "user", "content": "synthetic"}],
                    }
                    response = await client.post("/v1/messages", json=body)
                    assert response.status == 200
                    assert response.headers[ingress.LANE_HEADER] == "anthropic"
                    assert await response.content.readexactly(len(first)) == first
                    session = client.app[ingress._SESSION_KEY]
                    pins = ingress._session_lane
                    state = ingress.lane_state
                    assert pins == {"synthetic-session": "anthropic"}
                    _policy(registry, [])
                    await ingress._reload_provider_registry()
                    assert ingress.LANES["anthropic"] is lane
                    assert client.app[ingress._SESSION_KEY] is session and not session.closed
                    assert ingress._session_lane is pins and pins == {
                        "synthetic-session": "anthropic"
                    }
                    assert ingress.lane_state is state and state["anthropic"].open
                    async with client.get("/__throttle/health") as health:
                        assert (await health.json())["provider_registry"] == {
                            "enabled": True,
                            "lanes": [],
                            "error": None,
                        }
                    async with client.post("/v1/messages", json=body) as held:
                        assert held.status == 403
                        assert (await held.json())["error"] == "ingress-session-lane-not-registered"
                    async with client.post(
                        "/v1/messages", json={**body, "metadata": {"user_id": "new-session"}}
                    ) as denied:
                        assert denied.status == 503
                        assert (await denied.json())["error"] == "ingress-no-registered-lane"
                    assert len(hits) == 1 and not response.content.at_eof()
                    release.set()
                    assert await response.read() == last
                    _policy(registry, ["anthropic"])
                    await ingress._reload_provider_registry()
                    async with client.post("/v1/messages", json=body) as restored:
                        assert restored.status == 200
                        assert restored.headers[ingress.LANE_HEADER] == "anthropic"
                        assert await restored.read() == first + last
                    assert hits == [body, body]
                    assert ingress._session_lane is pins
            finally:
                release.set()
