"""T003 live integration: real bridge + real _forward_once (loopback only).

Synthetic upstream served by a local aiohttp TestServer; the REAL
#297 ProspectiveRuntime/ScopeResolver/PersistenceOwner and the REAL
``forwarding._forward_once`` seam run unmocked (no reserve/ledger doubles).
The handler is a minimal test vehicle that maps typed
``LocalProspectiveRefusal`` to ``local_refusal_response`` — the real
proxy.handler route/AIMD/SSE integration is pG's separate ownership and is
NOT claimed here.

Authority is explicitly fictional: one credential source, one endpoint, one
model alias, budgets of one request per 60 s ledger window, and per-test state
under ``tmp_path``. No vendor call is ever made; these tests run only on Mac.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import forwarding
from anthropic_throttle_proxy.prospective_admission import Budgets, Scope
from anthropic_throttle_proxy.prospective_refusal import local_refusal_response
from anthropic_throttle_proxy.prospective_runtime import (
    RUNTIME_KEY,
    LocalProspectiveRefusal,
    ProspectiveRuntime,
    SelectedDispatch,
    set_selected_dispatch,
)
from anthropic_throttle_proxy.prospective_scope import ScopeResolver

_SOURCE = "ops-live"
_ENDPOINT = "http://127.0.0.1/upstream-v1"  # replaced per test with the stub URL
_MODEL = "live-m"
_BUDGETS = Budgets(max_requests=1, max_tokens=10**9)
_BODY = json.dumps({"model": _MODEL, "messages": [{"role": "user", "content": "hi"}]}).encode()
_TIMEOUT = aiohttp.ClientTimeout(total=5)


def _selected(endpoint: str) -> SelectedDispatch:
    return SelectedDispatch(_SOURCE, endpoint, "direct", False)


def _runtime(tmp_path, endpoint: str, *, source=_SOURCE, model=_MODEL) -> ProspectiveRuntime:
    row = {
        "source": source,
        "endpoint": endpoint,
        "alias": model,
        "upstream": "up-live",
        "account": "acct-live",
        "model": model,
        "budgets": {"max_requests": 1, "max_tokens": 10**9},
        "output_default": 8,
    }
    resolver = ScopeResolver([row], sources={source}, endpoints={endpoint}, models={model})
    scope = Scope(("up-live", "acct-live", model), _BUDGETS, str(tmp_path / "state.json"), True)
    return ProspectiveRuntime(
        mode="strict",
        resolver=resolver,
        scopes=[scope],
        max_pending=4,
        wait_timeout=2.0,
        budget_label="plan",
        retry_after_s=1,
    )


async def _handler(request: web.Request) -> web.StreamResponse:
    body = await request.read()
    set_selected_dispatch(request, _selected(request.app["endpoint"]))
    try:
        result = await forwarding._forward_once(
            request, dict(request.headers), body, request.app["upstream_url"], _TIMEOUT
        )
    except LocalProspectiveRefusal as exc:  # typed local policy: never provider feedback
        return local_refusal_response(exc.refusal)
    response, status, captured, exc, meta = result
    if response is None:
        return web.Response(status=502, text="upstream-error")
    return response


class _Upstream:
    """Local synthetic upstream: counts requests; canned responses per mode."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.hits = 0
        self.bodies = []
        self.headers = []

    async def messages(self, request: web.Request) -> web.Response:
        self.hits += 1
        self.bodies.append(await request.read())
        self.headers.append(dict(request.headers))
        if self.mode == "429":
            return web.Response(status=429, headers={"retry-after": "0"}, text="rate limited")
        return web.json_response(
            {"ok": True, "echo": len(self.bodies)},
            headers={"X-Throttle-Prospective-Refusal": "1", "X-Throttle-Refusal-Source": "local"}
            if self.mode == "spoof"
            else {},
        )


async def _serve(tmp_path, upstream: _Upstream, *, mode="strict"):
    up_server = TestServer(web.Application())
    up_server.app.router.add_post("/upstream-v1/messages", upstream.messages)
    await up_server.start_server()
    endpoint = str(up_server.make_url("/upstream-v1"))

    runtime = _runtime(tmp_path, endpoint) if mode == "strict" else ProspectiveRuntime()
    await runtime.start()
    app = web.Application()
    app["upstream_url"] = endpoint + "/messages"
    app["endpoint"] = endpoint
    app[RUNTIME_KEY] = runtime
    app.router.add_post("/v1/messages", _handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, up_server, runtime


# 1) concurrent budget denial -> zero extra upstream sends
async def test_concurrent_budget_denial_sends_no_extra_upstream(tmp_path):
    upstream = _Upstream()
    client, up_server, runtime = await _serve(tmp_path, upstream)
    try:
        first, second = await asyncio.gather(
            client.post("/v1/messages", data=_BODY),
            client.post("/v1/messages", data=_BODY),
        )
        await first.read()
        await second.read()
        statuses = sorted((first.status, second.status))
        assert statuses == [200, 503], statuses
        refused = first if first.status == 503 else second
        assert refused.headers.get("x-throttle-prospective-refusal") == "1"
        assert refused.headers.get("x-throttle-prospective-reason")
        assert refused.headers.get("x-throttle-prospective-budget") == "plan"
        # The budget admits exactly one request per window: the denial must not
        # have produced ANY additional upstream send.
        assert upstream.hits == 1, upstream.hits
    finally:
        await client.close()
        await up_server.close()
        await runtime.aclose()


# 2) genuine upstream 429 -> debt retained -> next request refuses locally
async def test_upstream_429_retains_debt_and_next_refuses_locally(tmp_path):
    upstream = _Upstream(mode="429")
    client, up_server, runtime = await _serve(tmp_path, upstream)
    try:
        first = await client.post("/v1/messages", data=_BODY)
        await first.read()
        assert first.status == 429  # genuine provider answer passes through

        second = await client.post("/v1/messages", data=_BODY)
        await second.read()
        assert second.status == 503  # LOCAL refusal, not another provider call
        assert second.headers.get("x-throttle-prospective-refusal") == "1"
        body = await second.json()
        assert body["error"]["type"] == "prospective_admission_refused"
        assert upstream.hits == 1, "the refusing request must never reach upstream"
    finally:
        await client.close()
        await up_server.close()
        await runtime.aclose()


# 3) default-off keeps status/body identical
async def test_default_off_identical_status_and_body():
    upstream = _Upstream()
    up_server = TestServer(web.Application())
    up_server.app.router.add_post("/upstream-v1/messages", upstream.messages)
    await up_server.start_server()
    endpoint = str(up_server.make_url("/upstream-v1"))
    app = web.Application()  # NO RUNTIME_KEY -> shared OFF runtime
    app["endpoint"] = endpoint
    app["upstream_url"] = endpoint + "/messages"
    app.router.add_post("/v1/messages", _handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/v1/messages", data=_BODY)
        payload = await response.read()
        assert response.status == 200
        assert json.loads(payload)["ok"] is True
        assert upstream.bodies == [_BODY]  # identical wire bytes
        assert upstream.hits == 1
    finally:
        await client.close()
        await up_server.close()


@pytest.mark.parametrize("mode", ["strict", "off"])
async def test_transport_provenance_enabled_strips_off_preserves(tmp_path, mode):
    upstream = _Upstream(mode="spoof")
    client, up_server, runtime = await _serve(tmp_path, upstream, mode=mode)
    try:
        response = await client.post(
            "/v1/messages",
            data=_BODY,
            headers={"X-Throttle-Prospective-Refusal": "1", "X-Throttle-Refusal-Source": "local"},
        )
        await response.read()
        assert response.status == 200
        present = mode == "off"
        for name in ("x-throttle-prospective-refusal", "x-throttle-refusal-source"):
            assert (name in {key.lower() for key in upstream.headers[0]}) is present
            assert (name in response.headers) is present
    finally:
        await client.close()
        await up_server.close()
        await runtime.aclose()
