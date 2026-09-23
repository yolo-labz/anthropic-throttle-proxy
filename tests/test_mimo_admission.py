"""Exact headerless MiMo 429 shape, using only a local fake upstream."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, forwarding, proxy

ERROR = {"code": "429", "message": "Too many requests", "type": "limitation"}
SSE = b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


@pytest.mark.parametrize(
    "path,expected",
    [
        ("v1/messages", True),
        ("api/anthropic/v1/messages", True),
        ("v1/chat/completions", True),
        ("api/coding/paas/v4/chat/completions", True),
        ("v1/responses", True),
        ("/v1/chat/completions/", True),
        ("api/oauth/usage", False),
        ("api/oauth/profile", False),
        ("v1/models", False),
        ("v1/messages/count_tokens", False),
        ("v1/responses/req_123", False),
        ("not-v1/messages", False),
    ],
)
def test_shared_pause_covers_generation_not_telemetry(path, expected):
    assert proxy._retry_after_blocks_path(path) is expected


@pytest.mark.parametrize(
    "mode,first_pushback,shared_backoff",
    [("off", False, False), ("fair", False, False), ("fair", True, False), ("fair", True, True)],
)
async def test_openai_burst_is_admitted_before_upstream_not_retried_as_a_herd(
    monkeypatch, proxy_admission_state, mode, first_pushback, shared_backoff
):
    active = peak = refused = calls = 0
    sent_at = []
    body = b'{"model":"mimo-v2.6-pro","stream":true,"messages":[{"role":"user","content":"test"}]}'

    async def upstream(request):
        nonlocal active, peak, refused, calls
        calls += 1
        sent_at.append(asyncio.get_running_loop().time())
        assert request.path == "/v1/chat/completions"
        assert await request.read() == body
        if (first_pushback and calls == 1) or active >= 2:
            refused += 1
            return web.json_response(ERROR, status=429)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.05)
            return web.Response(body=SSE, content_type="text/event-stream")
        finally:
            active -= 1

    remote = web.Application()
    remote.router.add_post("/v1/chat/completions", upstream)
    async with TestServer(remote) as origin:
        for key, value in {
            "UPSTREAM": str(origin.make_url("")).rstrip("/"),
            "CENTRAL_URL": "",
            "QUEUE_MODE": mode,
            "MAX_CONCURRENT": 2,
            "AIMD_INITIAL_CONCURRENT": 2,
            "PRIORITY_RESERVE_SLOTS": 0,
            "RATE_PUSHBACK_RETRIES": int(first_pushback and not shared_backoff),
            "AIMD_BACKOFF_S": 0.05,
            "MIN_DISPATCH_GAP_S": 0,
            "QUEUE_MAX_WAIT_S": 3,
        }.items():
            monkeypatch.setattr(config, key, value)
        monkeypatch.setenv("THROTTLE_ACCOUNT_ROUTING", "off")
        app = web.Application()
        app.on_response_prepare.append(forwarding.stamp_proxy_marker)
        app.router.add_route("*", "/{path:.*}", proxy.handler)
        async with TestClient(TestServer(app)) as client:

            async def call():
                async with client.post(
                    "/v1/chat/completions",
                    data=body,
                    headers={"Authorization": "Bearer test-key"},
                ) as response:
                    return response.status, await response.read()

            if shared_backoff:
                status, _ = await call()
                assert status == 429
            results = await asyncio.gather(*(call() for _ in range(6)))
            if shared_backoff:
                assert sent_at[1] - sent_at[0] >= 0.045, "sibling bypassed shared cooldown"
        if mode == "off":
            assert refused > 0  # cold-start probation may serialize the first call only
            assert sum(status == 429 for status, _ in results) == refused
        else:
            assert refused == int(first_pushback)
            assert calls == 6 + int(first_pushback)
            assert results == [(200, SSE)] * 6
        assert 1 <= peak <= 2
        if not first_pushback:
            assert peak == 2
        assert config.state["inflight"] == config.state["queued"] == 0
