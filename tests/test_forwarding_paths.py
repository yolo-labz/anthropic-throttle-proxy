"""Coverage for the harder forwarding branches: central failover, the central
health loop, burst pacing, the 502 exhausted-retry path, and the advisor UI
route when enabled. These exercise code the happy-path suite doesn't reach,
without changing any runtime behaviour.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, forwarding, limiter, pacing, proxy
from anthropic_throttle_proxy.ui import advisor_impl
from anthropic_throttle_proxy.ui.routes import attach_ui

_SSE_BODY = (
    b'event: message_start\ndata: {"type":"message_start","message":'
    b'{"usage":{"input_tokens":4}}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _reset() -> None:
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    config.state.update(
        {
            "inflight": 0,
            "queued": 0,
            "served": 0,
            "client_disconnects": 0,
            "upstream_retries": 0,
            "central_status": "unknown",
            "central_last_check": 0,
            "last_advisor": None,
        }
    )


def _ok_upstream() -> web.Application:
    async def messages(request: web.Request) -> web.StreamResponse:
        await request.read()
        resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_SSE_BODY)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", messages)
    return app


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


@pytest.fixture
async def env(monkeypatch):
    """Live stub upstream + a bound, route-wired proxy app + TestClient."""
    upstream = TestServer(_ok_upstream())
    await upstream.start_server()
    upstream_url = str(upstream.make_url("")).rstrip("/")

    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "UPSTREAM", upstream_url)
    _reset()

    app = web.Application()
    app.router.add_get("/__throttle/health", proxy.health)
    app.router.add_get("/metrics", proxy.metrics)
    attach_ui(app)
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    server = TestServer(app)
    tc = TestClient(server)
    await tc.start_server()
    try:
        yield tc, upstream_url
    finally:
        await tc.close()
        await upstream.close()


async def test_central_failover_marks_down_and_retries_direct(env, monkeypatch) -> None:
    tc, upstream_url = env
    # Pretend a central tier is configured + healthy, but point it at a dead
    # port so the forward fails → handler marks it DOWN and retries direct.
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")  # unroutable
    config.state["central_status"] = "up"
    resp = await tc.post(
        "/v1/messages",
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer central-fail"},
    )
    body = await resp.read()
    await _settle()
    assert resp.status == 200  # direct retry succeeded against the stub
    assert b"message_stop" in body
    assert config.state["central_status"] == "down"
    assert config.state["upstream_retries"] >= 1


async def test_central_down_off_mode_uses_local_fair_queue(env, monkeypatch) -> None:
    tc, _ = env
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")
    config.state["central_status"] = "down"

    resp = await tc.post(
        "/v1/messages",
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer central-down"},
    )
    await resp.read()
    await _settle()

    assert resp.status == 200
    bid = next(iter(config.bearer_limiters))
    lim = config.bearer_limiters[bid]
    assert lim.queue_mode == "fair"
    assert lim.queue_enabled is True
    assert lim.observe_enabled is True
    assert lim.hard_max == config.CENTRAL_LOCAL_MAX_CONCURRENT


async def test_central_up_off_mode_still_uses_local_safety_queue(env, monkeypatch) -> None:
    tc, upstream_url = env
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 32)
    monkeypatch.setattr(config, "CENTRAL_LOCAL_MAX_CONCURRENT", 2)
    monkeypatch.setattr(config, "CENTRAL_URL", upstream_url)
    config.state["central_status"] = "up"

    resp = await tc.post(
        "/v1/messages",
        data=b'{"model":"claude-opus-4-7[1m]"}',
        headers={"Authorization": "Bearer central-up"},
    )
    await resp.read()
    await _settle()

    assert resp.status == 200
    bid = next(iter(config.bearer_limiters))
    lim = config.bearer_limiters[bid]
    assert lim.queue_mode == "fair"
    assert lim.queue_enabled is True
    assert lim.hard_max == 2
    assert lim.max_concurrent == 2


async def test_fallback_promotion_does_not_downgrade_on_central_recovery(env, monkeypatch) -> None:
    tc, _ = env
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")
    config.state["central_status"] = "down"

    resp = await tc.post(
        "/v1/messages",
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer sticky-fair"},
    )
    await resp.read()
    await _settle()

    config.state["central_status"] = "up"
    resp = await tc.post(
        "/v1/messages",
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer sticky-fair"},
    )
    await resp.read()
    await _settle()

    bid = next(iter(config.bearer_limiters))
    assert config.bearer_limiters[bid].queue_mode == "fair"


async def test_central_failure_direct_retry_is_serialized(monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(config, "UPSTREAM", "http://direct.example")
    monkeypatch.setattr(proxy, "_direct_fallback_lock", None)
    config.state["central_status"] = "up"
    config.state["upstream_retries"] = 0

    active = 0
    max_active = 0

    async def fake_try_forward(_request, _headers, _body, _url, _timeout, attempt):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        attempt.final_status = 200
        response = web.Response(status=200, text="ok")
        attempt.response = response
        return response, None

    monkeypatch.setattr(proxy, "_try_forward", fake_try_forward)
    timeout = aiohttp.ClientTimeout(total=1)

    class FakeRequest:
        query_string = ""

    async def one_retry() -> web.StreamResponse | web.Response:
        return await proxy._retry_direct_once(
            FakeRequest(),
            {},
            None,
            "v1/messages",
            "central",
            "http://127.0.0.1:1/v1/messages",
            timeout,
            RuntimeError("central down"),
            proxy._Attempt(),
        )

    responses = await asyncio.gather(*(one_retry() for _ in range(4)))

    assert [r.status for r in responses] == [200, 200, 200, 200]
    assert max_active == 1
    assert config.state["upstream_retries"] == 4


async def test_exhausted_retry_returns_502(env, monkeypatch) -> None:
    tc, _ = env
    # No central; point the direct upstream at a dead port so BOTH the initial
    # attempt and the single direct retry fail → final 502.
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "UPSTREAM", "http://127.0.0.1:1")
    resp = await tc.post(
        "/v1/messages",
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer dead-upstream"},
    )
    text = await resp.text()
    await _settle()
    assert resp.status == 502
    assert "upstream error" in text
    assert config.state["upstream_retries"] >= 1


async def test_pace_dispatch_enforces_gap(monkeypatch) -> None:
    pacing.set_lock(asyncio.Lock())
    pacing._last_dispatch_ts = 0.0
    monkeypatch.setattr(config, "MIN_DISPATCH_GAP_S", 0.05)
    # First call sets the timestamp (no wait); second must wait ~50 ms.
    await pacing._pace_dispatch()
    t0 = asyncio.get_running_loop().time()
    await pacing._pace_dispatch()
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed >= 0.04


async def test_pace_dispatch_noop_when_disabled(monkeypatch) -> None:
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "MIN_DISPATCH_GAP_S", 0.0)
    t0 = asyncio.get_running_loop().time()
    await pacing._pace_dispatch()
    assert asyncio.get_running_loop().time() - t0 < 0.02


async def test_central_health_loop_noop_without_central(monkeypatch) -> None:
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    # Returns immediately — must not raise or hang.
    await asyncio.wait_for(forwarding.central_health_loop(), timeout=1.0)


async def test_poll_central_once_up_then_down() -> None:
    health_app = web.Application()

    async def ok_health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    health_app.router.add_get("/__throttle/health", ok_health)
    server = TestServer(health_app)
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    try:
        timeout = aiohttp.ClientTimeout(total=2)
        config.CENTRAL_URL = url
        config.state["central_status"] = "unknown"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await forwarding._poll_central_once(session)
        assert config.state["central_status"] == "up"
    finally:
        await server.close()
        config.CENTRAL_URL = ""

    # Now an unreachable central → DOWN.
    config.CENTRAL_URL = "http://127.0.0.1:1"
    timeout = aiohttp.ClientTimeout(total=0.3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await forwarding._poll_central_once(session)
    assert config.state["central_status"] == "down"
    config.CENTRAL_URL = ""


async def test_ui_advisor_enabled_returns_recommendation(env, monkeypatch) -> None:
    tc, _ = env
    monkeypatch.setenv("ADVISOR_ENABLED", "true")

    async def fake_recommend(_snapshot):
        return "raise THROTTLE_MIN_DISPATCH_GAP_MS to 50"

    monkeypatch.setattr(advisor_impl, "recommend", fake_recommend)
    resp = await tc.post("/ui/advisor")
    assert resp.status == 200
    assert "THROTTLE_MIN_DISPATCH_GAP_MS" in await resp.text()


async def test_ui_advisor_enabled_surfaces_error(env, monkeypatch) -> None:
    tc, _ = env
    monkeypatch.setenv("ADVISOR_ENABLED", "true")

    async def boom(_snapshot):
        raise RuntimeError("groq exploded")

    monkeypatch.setattr(advisor_impl, "recommend", boom)
    resp = await tc.post("/ui/advisor")
    assert resp.status == 500
    assert "advisor error" in await resp.text()
