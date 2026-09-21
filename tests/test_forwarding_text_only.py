"""Actual handler/HTTP forwarding with synthetic provider URLs mapped to loopback."""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from anthropic_throttle_proxy import config, limiter, pacing, proxy

PATH = "/api/coding/paas/v4/chat/completions"
RAW = json.dumps(
    {
        "messages": [
            {"role": "user", "content": "KEEP THIS REQUEST"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "hidden"}]},
        ]
    }
).encode()
EXPECTED = [
    {"role": "user", "content": "KEEP THIS REQUEST"},
    {"role": "assistant", "content": "[no text content]"},
]


@pytest.fixture
async def wire(monkeypatch):
    """Run real local HTTP; never resolve or contact a provider/production service."""
    seen = []
    attempts = []

    async def echo(request):
        body = await request.read()
        seen.append(
            (request.method, request.path, body, request.headers.getall("Content-Length", []))
        )
        if request.path.startswith("/central/"):
            return web.Response(status=502, text="synthetic central failure")
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", echo)
    upstream = TestServer(app)
    await upstream.start_server()
    original = aiohttp.ClientSession._request

    async def local_request(self, method, str_or_url, **kwargs):
        url = URL(str_or_url)
        if url.host in {"api.z.ai", "api.openai.com", "api.anthropic.com", "central.example.test"}:
            attempts.append((str(url), kwargs.get("data"), kwargs.get("headers", {})))
            path = ("/central" if url.host == "central.example.test" else "") + url.path_qs
            url = upstream.make_url(path)
        assert url.host == "127.0.0.1", "test must never contact a real upstream"
        return await original(self, method, url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", local_request)
    monkeypatch.setattr(config, "UPSTREAM", "https://api.z.ai")
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    monkeypatch.setattr(config, "MIN_DISPATCH_GAP_S", 0)
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setitem(config.state, "central_status", "unknown")
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, seen, attempts
    finally:
        await client.close()
        await upstream.close()


async def test_direct_chat_request_reaches_wire_normalized(wire):
    client, seen, _ = wire
    async with client.post(PATH, data=RAW, headers={"content-length": str(len(RAW))}) as response:
        assert response.status == 200
        assert (await response.json())["messages"] == EXPECTED
    assert json.loads(seen[-1][2])["messages"] == EXPECTED
    assert seen[-1][3] == [str(len(seen[-1][2]))]


async def test_central_failure_normalizes_only_direct_retry(wire, monkeypatch):
    client, seen, attempts = wire
    monkeypatch.setattr(config, "CENTRAL_URL", "https://central.example.test")
    monkeypatch.setitem(config.state, "central_status", "up")
    async with client.post(PATH, data=RAW) as response:
        assert response.status == 200
        assert (await response.json())["messages"] == EXPECTED
    assert len(attempts) == 2
    assert attempts[0][1] == RAW  # unknown central contract: preserve original input
    assert json.loads(attempts[1][1])["messages"] == EXPECTED
    # Handler strips incoming Content-Length; normalizing the retry must not
    # add one to the original header mapping retained by the central attempt.
    assert not any(k.lower() == "content-length" for k in attempts[0][2])
    assert seen[0][3] == [str(len(RAW))]
    assert seen[-1][3] == [str(len(seen[-1][2]))]


@pytest.mark.parametrize(
    ("base", "path", "method"),
    [
        ("https://api.openai.com", "/v1/chat/completions", "POST"),
        ("https://api.anthropic.com", "/v1/messages", "POST"),
        ("https://api.z.ai", "/api/anthropic/v1/messages", "POST"),
        ("https://api.z.ai", PATH, "PUT"),
    ],
)
async def test_unrelated_provider_protocol_or_method_unchanged(
    wire, monkeypatch, base, path, method
):
    client, seen, _ = wire
    monkeypatch.setattr(config, "UPSTREAM", base)
    async with client.request(method, path, data=RAW) as response:
        assert response.status == 200
        assert await response.read() == RAW
    assert seen[-1][2] == RAW


async def test_malformed_nested_block_does_not_turn_forward_into_500(wire):
    client, seen, _ = wire
    raw = b'{"messages":[{"role":"user","content":[{"type":"text","text":123}]}]}'
    async with client.post(PATH, data=raw) as response:
        assert response.status == 200
        assert await response.read() == raw
    assert seen[-1][2] == raw
