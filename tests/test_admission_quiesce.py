"""Race-free admission quiesce: close -> drain -> swap -> reopen (issue #300).

Proves the property the idle-gate-then-SIGTERM sequence could NOT guarantee: a
request arriving in the exact gap between the drain observation and the stop is
refused at the gate (zero upstream, no state), while in-flight work completes.
Control surfaces stay answerable while closed (drain polling + reopen), and
reopen/rollback is a plain resume.

Reuses the ``test_keepalive_hold`` client harness (real ``proxy.handler`` app);
the upstream is a local counting stub with a hold event (loopback only, no
vendor calls). ``quiesce``/``resume``/``admission`` are invoked as their route
handlers directly — ``main()`` owns route registration.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from anthropic_throttle_proxy import config, proxy
from test_keepalive_hold import _make_client_with_upstream


class _CountingUpstream:
    def __init__(self):
        self.hits = 0
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def messages(self, request: web.Request) -> web.Response:
        self.hits += 1
        self.entered.set()
        await request.read()
        await self.release.wait()
        return web.json_response({"ok": True})


async def _make_client(monkeypatch, upstream: _CountingUpstream):
    up_app = web.Application()
    up_app.router.add_route("*", "/{prefix:.*}v1/messages", upstream.messages)
    up_app.router.add_route("*", "/v1/messages", upstream.messages)
    up_app.router.add_route("*", "/{path:.*}", upstream.messages)
    client, up_server = await _make_client_with_upstream(monkeypatch, up_app)
    await proxy.resume(None)  # deterministic start state (module-global flag)
    return client, up_server


_BODY = json.dumps({"model": "claude-opus-4-8", "max_tokens": 16, "messages": []}).encode()
_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer test-quiesce"}


async def test_request_in_the_quiesce_gap_is_refused_and_inflight_completes(monkeypatch):
    """THE gap test: a new request arrives after quiesce and before any stop —
    it must be refused at the gate while the earlier in-flight request completes
    untouched, and the drained state must STAY closed (no reopen race)."""
    upstream = _CountingUpstream()
    client, up_server = await _make_client(monkeypatch, upstream)
    try:
        inflight = asyncio.create_task(client.post("/v1/messages", data=_BODY, headers=_HEADERS))
        await asyncio.wait_for(upstream.entered.wait(), timeout=2)
        assert upstream.hits == 1

        # Close admission atomically; the in-flight request keeps running.
        closed = await proxy.quiesce(None)
        assert closed.body and json.loads(closed.body)["admission"] == "closed"

        # A NEW request arrives in the gap between "drain observation" and stop.
        gap = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await gap.read()
        assert gap.status == 503
        assert gap.headers.get("x-throttle-admission-closed") == "1"
        assert upstream.hits == 1, "gap request must never reach upstream"
        assert config.state["served"] == 0

        # The preserved work completes normally (nothing cut).
        upstream.release.set()
        response = await asyncio.wait_for(inflight, timeout=5)
        await response.read()
        assert response.status == 200
        assert upstream.hits == 1

        # Drain observation converges — and STAYS converged at the swap point.
        for _ in range(20):
            await asyncio.sleep(0)
        assert config.state["inflight"] == 0
        assert config.state["queued"] == 0
        assert config.state["keepalive_holds_active"] == 0
        late = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await late.read()
        assert late.status == 503
        assert upstream.hits == 1

        # Reopen (= rollback path) restores service.
        opened = await proxy.resume(None)
        assert json.loads(opened.body)["admission"] == "open"
        ok = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await ok.read()
        assert ok.status == 200
        assert upstream.hits == 2
    finally:
        await client.close()
        await up_server.close()


async def test_quiesce_keeps_control_surfaces_answerable(monkeypatch):
    """Drain polling and reopen must work while admission is closed."""
    upstream = _CountingUpstream()
    upstream.release.set()
    client, up_server = await _make_client(monkeypatch, upstream)
    try:
        await proxy.quiesce(None)
        health = await client.get("/__throttle/health")
        assert health.status == 200
        admission = await proxy.admission(None)
        assert admission.status == 200
        refused = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await refused.read()
        assert refused.status == 503
        assert refused.headers.get("retry-after")
        assert upstream.hits == 0
        assert config.state["served"] == 0
    finally:
        await client.close()
        await up_server.close()


async def test_quiesce_and_resume_are_idempotent(monkeypatch):
    upstream = _CountingUpstream()
    upstream.release.set()
    client, up_server = await _make_client(monkeypatch, upstream)
    try:
        for _ in range(2):
            assert json.loads((await proxy.quiesce(None)).body)["admission"] == "closed"
        refused = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await refused.read()
        assert refused.status == 503
        for _ in range(2):
            assert json.loads((await proxy.resume(None)).body)["admission"] == "open"
        ok = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await ok.read()
        assert ok.status == 200
    finally:
        await client.close()
        await up_server.close()
