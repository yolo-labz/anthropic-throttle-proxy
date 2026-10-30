"""W1: inbound GET /api/oauth/{usage,profile} is single-flight + TTL-cached per bearer.

Falsifier: N concurrent inbound polls for one bearer cause <= 1 upstream request per
OAUTH_TELEMETRY_TTL_S, and a 429 from upstream suppresses further upstream calls.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, proxy


def _upstream(counter: dict[str, int], status: dict[str, int]) -> web.Application:
    async def usage(request: web.Request) -> web.Response:
        counter["n"] = counter.get("n", 0) + 1
        await asyncio.sleep(0.05)  # widen the window so concurrent callers overlap
        if status["code"] != 200:
            return web.Response(status=status["code"], text="{}")
        return web.json_response({"five_hour": {"utilization": 42}})

    app = web.Application()
    app.router.add_get("/{path:.*}", usage)
    return app


@pytest.fixture
async def client(monkeypatch):
    counter: dict[str, int] = {}
    status = {"code": 200}
    upstream = TestServer(_upstream(counter, status))
    await upstream.start_server()
    monkeypatch.setattr(config, "UPSTREAM", str(upstream.make_url("")).rstrip("/"))
    proxy._oauth_telemetry_cache.clear()
    proxy._oauth_telemetry_fail.clear()
    proxy._oauth_telemetry_locks.clear()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    cli = TestClient(TestServer(app))
    await cli.start_server()
    try:
        yield cli, counter, status
    finally:
        await cli.close()
        await upstream.close()


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_concurrent_polls_make_one_upstream_call(client):
    cli, counter, _ = client
    resps = await asyncio.gather(
        *(cli.get("/api/oauth/usage", headers=_hdr("tokA")) for _ in range(14))
    )
    bodies = [await r.read() for r in resps]
    assert [r.status for r in resps] == [200] * 14
    assert len(set(bodies)) == 1
    assert counter["n"] == 1
    # A second burst inside the TTL must not reach upstream again.
    await asyncio.gather(*(cli.get("/api/oauth/usage", headers=_hdr("tokA")) for _ in range(5)))
    assert counter["n"] == 1


async def test_each_bearer_is_its_own_account(client):
    cli, counter, _ = client
    await asyncio.gather(
        cli.get("/api/oauth/usage", headers=_hdr("tokA")),
        cli.get("/api/oauth/usage", headers=_hdr("tokB")),
    )
    assert counter["n"] == 2


async def test_upstream_429_backs_off_without_hammering(client):
    cli, counter, status = client
    status["code"] = 429
    first = await cli.get("/api/oauth/usage", headers=_hdr("tokC"))
    assert first.status == 429  # first failure is relayed unchanged
    for _ in range(10):
        r = await cli.get("/api/oauth/usage", headers=_hdr("tokC"))
        assert r.status == 429 and int(r.headers["Retry-After"]) >= 80
    assert counter["n"] == 1


async def test_ttl_expiry_allows_exactly_one_refetch(client, monkeypatch):
    cli, counter, _ = client
    monkeypatch.setattr(proxy, "OAUTH_TELEMETRY_TTL_S", 0.05)
    await cli.get("/api/oauth/usage", headers=_hdr("tokD"))
    await asyncio.sleep(0.1)
    await cli.get("/api/oauth/usage", headers=_hdr("tokD"))
    assert counter["n"] == 2
