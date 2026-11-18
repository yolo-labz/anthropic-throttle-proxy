"""A short cooldown on an already-serving bearer must park, not churn slots."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, proxy

# The 429's pause, in real seconds. It must outlast the setup between the 429
# and the third call on a loaded host: at 0.08 s, 16 CPU burners on one core let
# it lapse first (`retry_after_remaining() == 0.0`, or the "expired" call served
# 200). A budget below it expires; one above outlasts it.
PAUSE_S = 0.4


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("max_wait", [0.02, 1.0])
async def test_warm_bearer_short_cooldown_waits_before_slot(
    monkeypatch, proxy_admission_app, path, max_wait
):
    calls = 0
    lines = []

    async def upstream(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            return web.json_response(
                {"code": "429", "message": "Too many requests", "type": "limitation"},
                status=429,
            )
        return web.json_response({"ok": True})

    remote = web.Application()
    remote.router.add_post(path, upstream)
    async with TestServer(remote) as origin:
        for key, value in {
            "UPSTREAM": str(origin.make_url("")).rstrip("/"),
            "CENTRAL_URL": "",
            "QUEUE_MODE": "fair",
            "MAX_CONCURRENT": 2,
            "AIMD_INITIAL_CONCURRENT": 2,
            "RATE_PUSHBACK_RETRIES": 0,
            "AIMD_BACKOFF_S": PAUSE_S,
            "MIN_DISPATCH_GAP_S": 0,
            # Unbounded for the two setup calls: the budget is END-TO-END from
            # handler start, so 20 ms of wall time was also charged to cold-start
            # probation, and CI run 251 got a queue-timeout 503 on the FIRST
            # call. The bound under test is set right before the third call.
            "QUEUE_MAX_WAIT_S": 0,
        }.items():
            monkeypatch.setattr(config, key, value)
        monkeypatch.setenv("THROTTLE_ACCOUNT_ROUTING", "off")
        monkeypatch.setattr(proxy, "log", lines.append)
        async with TestClient(TestServer(proxy_admission_app)) as client:

            async def call():
                async with client.post(
                    path,
                    json={"model": "test", "messages": [{"role": "user", "content": "test"}]},
                    headers={"Authorization": "Bearer test-warm-cooldown"},
                ) as response:
                    await response.read()
                    return response.status

            assert await call() == 200  # clear cold-start probation first
            assert await call() == 429  # short pause does not require a half-open probe
            bid = proxy._bearer_id({"Authorization": "Bearer test-warm-cooldown"})
            lim = config.bearer_limiters[bid]
            assert not lim.retry_probe_required()
            assert lim.retry_after_remaining() > 0
            lines.clear()
            monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", max_wait)
            status = await asyncio.wait_for(call(), timeout=2)

        expired = max_wait < PAUSE_S
        assert status == (503 if expired else 200)
        assert calls == (2 if expired else 3)
        # A sleeping request does not acquire/release slots, log fake dispatches,
        # or replace real service-time samples with thousands of near-zero holds.
        assert sum("queue+" in line for line in lines) == (0 if expired else 1)
        assert sum("start  " in line for line in lines) == (0 if expired else 1)
        assert config.state["inflight"] == config.state["queued"] == 0
        assert lim.snapshot()["inflight"] == lim.snapshot()["queued_total"] == 0
