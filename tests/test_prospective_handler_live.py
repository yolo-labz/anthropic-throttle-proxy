"""T003 handler-level acceptance over the REAL ``proxy.handler`` app.

Reuses the ``test_keepalive_hold`` harness (``_make_client_with_upstream`` wires
the actual ``proxy.handler`` catch-all + UI + lock bindings; ``_aimd_shrink_total``
is the proven shrink counter) and the REAL #297 runtime (no reserve/ledger
mocks). A tiny counting upstream stub stands in for the provider — zero upstream
transport is asserted by its hit count, and by `served`/`upstream_retries`
staying untouched.

Cases:
  - strict exhausted  -> typed local 503, zero upstream, no AIMD shrink/served/retry
  - strict unsupported (no trusted selected metadata) -> same contract
  - default-off positive control -> normal 200 passthrough
  - upstream 429 positive control -> genuine provider 429 DOES fire an AIMD shrink

The fixture replaces credential discovery with an explicit fictional label;
the actual production route records it before the real reservation and handler.
The central ``LocalProspectiveRefusal`` catch in ``_try_forward`` is the
coordinator's production change; these tests assert its contract and do not
modify production. Fictional authority only, state under ``tmp_path``; no
vendor calls; Mac runs these.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_keepalive_hold import (
    _aimd_shrink_total,
    _make_client_with_upstream,
    _make_proxy_app,
    _make_upstream_app,
)
from test_prospective_forwarding_live import _runtime as _strict_runtime

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.prospective_runtime import (
    RUNTIME_KEY,
    SelectedDispatch,
)

_SOURCE = "ops-handler"
_MODEL = "claude-opus-4-8"
_BODY = json.dumps(
    {"model": _MODEL, "stream": True, "messages": [{"role": "user", "content": "synthetic"}]}
).encode()
_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer test-live-handler"}
_UNIFIED_WARNING = {
    "anthropic-ratelimit-unified-status": "allowed_warning",
    "anthropic-ratelimit-unified-utilization": "0.95",
}


class _CountingUpstream:
    """Minimal local upstream that counts transport hits (no vendor call)."""

    def __init__(self):
        self.hits = 0

    async def messages(self, request: web.Request) -> web.Response:
        self.hits += 1
        await request.read()
        return web.Response(status=200, headers={"content-type": "application/json"}, text="ok")


def _counting_app() -> web.Application:
    upstream = _CountingUpstream()
    app = web.Application()
    app["upstream"] = upstream
    app.router.add_route("*", "/{path:.*}", upstream.messages)
    return app


async def _make_strict_client(monkeypatch, tmp_path, upstream_app, *, with_selected: bool):
    """Use the shared real-handler app with an explicit fictional credential."""
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    endpoint = str(upstream_server.make_url("")).rstrip("/")
    selected = SelectedDispatch(_SOURCE, endpoint, "direct", False) if with_selected else None

    from anthropic_throttle_proxy import limiter, pacing

    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "UPSTREAM", endpoint)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "KEEPALIVE_HOLD", True)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 5.0)
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    config.state.update({"inflight": 0, "queued": 0, "served": 0, "upstream_retries": 0})

    runtime = _strict_runtime(tmp_path, endpoint, source=_SOURCE, model=_MODEL)
    await runtime.start()

    # Only credential discovery is fictional; production _claim_route records
    # this explicit selection, and the actual handler/reservation remain intact.
    monkeypatch.setattr(
        proxy,
        "_route_account_if_enabled",
        lambda headers, bid, **kwargs: (bid, _SOURCE if with_selected else None),
    )

    app = _make_proxy_app()
    app[RUNTIME_KEY] = runtime
    proxy_server = TestServer(app)
    client = TestClient(proxy_server)
    await client.start_server()
    return client, upstream_server, runtime, selected


def _assert_local_refusal(resp: web.StreamResponse, body: dict, reason: str) -> None:
    assert resp.status == 503
    assert resp.headers.get("x-throttle-prospective-refusal") == "1"
    assert resp.headers.get("x-throttle-prospective-reason") == reason
    assert resp.headers.get("x-throttle-prospective-budget") == "plan"
    assert body["type"] == "error"
    assert body["error"]["type"] == "prospective_admission_refused"


@pytest.mark.parametrize("case", ["exhausted", "unsupported"])
async def test_strict_local_refusal_typed_503_zero_upstream(monkeypatch, tmp_path, case):
    upstream_app = _counting_app()
    client, upstream_server, runtime, selected = await _make_strict_client(
        monkeypatch, tmp_path, upstream_app, with_selected=(case == "exhausted")
    )
    try:
        if case == "exhausted":
            # Trusted fixture metadata + REAL bridge pre-consumes the one-request
            # window (handoff => debt retained): the handler request must refuse.
            async with runtime.reserve(selected, _BODY) as permit:
                permit.handoff()
        before = _aimd_shrink_total()
        resp = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        body = await resp.json()
        expected_reason = "exhausted" if case == "exhausted" else "unbound"
        _assert_local_refusal(resp, body, expected_reason)
        # Zero upstream transport, zero provider feedback, no retries.
        assert upstream_app["upstream"].hits == 0
        assert config.state["served"] == 0
        assert config.state["upstream_retries"] == 0
        assert _aimd_shrink_total() == before, "local refusal must never AIMD-shrink"
    finally:
        await client.close()
        await upstream_server.close()
        await runtime.aclose()


async def test_default_off_positive_control_passthrough(monkeypatch):
    upstream_app = _counting_app()
    client, upstream_server = await _make_client_with_upstream(monkeypatch, upstream_app)
    try:
        resp = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await resp.read()
        assert resp.status == 200  # untouched normal path
        assert upstream_app["upstream"].hits == 1
    finally:
        await client.close()
        await upstream_server.close()


async def test_upstream_429_positive_control_shrinks(monkeypatch):
    """Genuine provider 429 keeps its AIMD shrink (contrast with local refusal)."""
    upstream_app = _make_upstream_app(
        fail_count=9999, fail_status=429, fail_headers=dict(_UNIFIED_WARNING)
    )
    client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=0.2
    )
    monkeypatch.setattr(config, "KEEPALIVE_HOLD", False)
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    before = _aimd_shrink_total()
    try:
        resp = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await resp.read()
        for _ in range(20):
            await asyncio.sleep(0)
        assert resp.status == 429
        assert _aimd_shrink_total() > before, "genuine upstream 429 must AIMD-shrink"
    finally:
        await client.close()
        await upstream_server.close()
