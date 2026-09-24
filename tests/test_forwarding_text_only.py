"""Actual handler/HTTP forwarding with synthetic provider URLs mapped to loopback."""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from anthropic_throttle_proxy import config, limiter, pacing, proxy, routing
from anthropic_throttle_proxy.metrics import M_CHAT_BODY_FITTED

PATH = "/api/coding/paas/v4/chat/completions"
# The shipped server raises aiohttp's 1 MiB default to 128 MiB (ingress.py and
# proxy.py both do), so an oversize-body test must not be refused by the
# HARNESS' own limit and mistake that 413 for the code under test.
UPSTREAM_MAX_BODY = 128 * 1024 * 1024
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

    app = web.Application(client_max_size=UPSTREAM_MAX_BODY)
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
    app = web.Application(client_max_size=UPSTREAM_MAX_BODY)
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


# --- request budget: the 413 path this lane answers with an unpublished ceiling ---

BUDGET = 20_000  # clears the protected tail (~6 KB here); see the unit falsifiers
# Mirrors the shipped default (routing.CHAT_MAX_BODY_BYTES). Kept as a literal so
# the regression below fails on the pre-fix head for the REAL reason — an
# oversize body reaching the wire untouched — instead of a missing attribute.
DEFAULT_BUDGET = 1_800_000


def oversize(n=40, filler=1000):
    """A transcript past BUDGET with a small protected tail."""
    msgs = [{"role": "system", "content": "ANCHOR: the assignment, never dropped"}]
    msgs += [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * filler}
        for i in range(n)
    ]
    return json.dumps({"model": "glm-5.3", "messages": msgs}).encode()


async def test_oversize_direct_chat_request_reaches_wire_fitted(wire, monkeypatch):
    client, seen, attempts = wire
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    big = oversize()
    assert len(big) > BUDGET, "fixture must actually be over budget"
    fitted_before = M_CHAT_BODY_FITTED._value.get()
    async with client.post(PATH, data=big, headers={"content-length": str(len(big))}) as response:
        assert response.status == 200
        forwarded = json.loads(await response.read())
    on_wire = seen[-1][2]
    assert len(on_wire) <= BUDGET
    assert len(on_wire) < len(big)
    assert json.loads(on_wire)["messages"] == forwarded["messages"]
    assert "ANCHOR" in forwarded["messages"][0]["content"], "anchor survives the egress path"
    assert forwarded["messages"][-1]["content"].startswith("m39"), "live tail survives"
    # Clipping history must be countable, not silent.
    assert M_CHAT_BODY_FITTED._value.get() == fitted_before + 1
    # The rebind is load-bearing: a stale length would corrupt the request body.
    assert attempts[-1][2]["Content-Length"] == str(len(on_wire))
    assert seen[-1][3] == [str(len(on_wire))]


async def test_central_keeps_the_full_body_and_only_the_direct_retry_is_fitted(wire, monkeypatch):
    """Central's contract is unknown, so it sees the client's bytes; the retry is shaped."""
    client, seen, attempts = wire
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    monkeypatch.setattr(config, "CENTRAL_URL", "https://central.example.test")
    monkeypatch.setitem(config.state, "central_status", "up")
    big = oversize()
    fitted_before = M_CHAT_BODY_FITTED._value.get()
    async with client.post(PATH, data=big) as response:
        assert response.status == 200
    assert len(attempts) == 2
    assert attempts[0][1] == big, "central receives the original bytes"
    assert len(attempts[1][1]) <= BUDGET, "the direct retry is fitted"
    assert len(seen[-1][2]) <= BUDGET
    # Exactly one fit: the central attempt is a no-op on a non-Z.AI host.
    assert M_CHAT_BODY_FITTED._value.get() == fitted_before + 1


async def test_oversize_body_on_a_sibling_protocol_is_untouched(wire, monkeypatch):
    """Same host, different protocol: the Anthropic shape needs its own blocks."""
    client, seen, _ = wire
    monkeypatch.setattr(routing, "CHAT_MAX_BODY_BYTES", BUDGET)
    big = oversize()
    fitted_before = M_CHAT_BODY_FITTED._value.get()
    async with client.post("/api/anthropic/v1/messages", data=big) as response:
        assert response.status == 200
        assert await response.read() == big
    assert seen[-1][2] == big
    assert M_CHAT_BODY_FITTED._value.get() == fitted_before


async def test_default_budget_fits_what_the_unfixed_proxy_forwarded_whole(wire):
    """The 413 this fixes, at the shipped default: ~2.1 MB reached the wire as sent.

    Written against the DEFAULT budget with no knob and no symbol the pre-fix head
    lacks, so it fails there for the real reason: the body was forwarded whole.
    """
    client, seen, _ = wire
    big = oversize(n=520, filler=4000)
    assert len(big) > DEFAULT_BUDGET, "fixture must be over budget"
    async with client.post(PATH, data=big, headers={"content-length": str(len(big))}) as r:
        assert r.status == 200
    on_wire = seen[-1][2]
    assert len(on_wire) <= DEFAULT_BUDGET
    assert len(on_wire) < len(big)
    assert seen[-1][3] == [str(len(on_wire))]
