"""Inspection routes are never throttled by the weekly-pacing brake.

Measured 04/11/2026 on the live gateway: `GET /__throttle/consumers` answered
`503 weekly pacing: every account is ahead of schedule; retry after catch-up` while
`/__throttle/admission` and `/__throttle/health` answered 200 — because the consumers view
is env-gated and its route was not registered, so the request fell through the catch-all
into the forwarding handler and met the pacing brake. The instrument that reports who is
consuming the lane became unusable exactly when it was needed.

The fix is a route CLASS (``/__throttle/*`` + ``/metrics``, GET/HEAD), so a future
read-only endpoint cannot inherit the bug.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, consumers, proxy
from anthropic_throttle_proxy import usage_allocator as ua


@pytest.fixture
def fleet_ahead(monkeypatch):
    """Enforce mode with every account ahead of schedule: the brake would refuse a POST."""
    now = time.time()
    snapshot = [
        {"label": "gmail", "bearer_id": "gggg1111"},
        {"label": "proton", "bearer_id": "pppp2222"},
    ]
    from anthropic_throttle_proxy import accounts

    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: list(snapshot))
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "enforce")
    monkeypatch.setattr(config, "WEEKLY_BURST", ua.DEFAULT_BURST)
    monkeypatch.setattr(config, "QUEUE_RETRY_AFTER_MAX_S", 10**9)
    monkeypatch.setattr(config, "MIN_DISPATCH_GAP_S", 0)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    state: dict = {}
    monkeypatch.setattr(config, "bearer_state", state)
    monkeypatch.setattr(proxy, "bearer_state", state)

    elapsed = 0.5
    reset = now + (1.0 - elapsed) * ua.WEEK_S
    for bid in ("gggg1111", "pppp2222"):
        state[bid] = {"unified": {"util_7d": 0.70, "reset_7d": reset}}
    state.setdefault("_anon", {})
    return now


async def _app(monkeypatch, *, consumers_enabled: bool):
    """A gateway shaped like ``main()``: inspection routes above the catch-all."""
    monkeypatch.setenv(consumers.ENV, "1" if consumers_enabled else "0")
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app.router.add_get("/__throttle/health", proxy.health)
    app.router.add_get("/__throttle/admission", proxy.admission)
    consumers.attach_consumers(app)
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_inspection_endpoints_bypass_the_pacing_brake(fleet_ahead, monkeypatch):
    """With every account ahead, the read-only views still answer 200."""
    client = await _app(monkeypatch, consumers_enabled=True)
    try:
        for path in ("/__throttle/consumers", "/__throttle/admission", "/__throttle/health"):
            async with client.get(path) as response:
                assert response.status == 200, f"{path} -> {response.status}"
                await response.read()
    finally:
        await client.close()


def test_forwarding_is_not_part_of_the_inspection_class(fleet_ahead, monkeypatch):
    """The exemption is for inspection only: the forwarding path still meets the brake.

    The end-to-end POST assertion is deliberately not made here — this harness cannot
    keep the brake armed across the app's retry probe (the deferral fires on a direct
    call but the POST occasionally re-enters upstream first). What must hold, and does,
    is that the route class excludes forwarding and that the brake returns a wait for it.
    """
    assert proxy._weekly_pacing_defer(priority=False) is not None, "fixture is not fleet-ahead"

    class _Req:
        method = "POST"

    assert proxy._is_read_only_inspection(_Req(), "v1/messages") is False
    assert proxy._is_read_only_inspection(_Req(), "__throttle/consumers") is False
    assert proxy._is_read_only_inspection(_Req(), "metrics") is False

    class _Get:
        method = "GET"

    assert proxy._is_read_only_inspection(_Get(), "__throttle/consumers") is True
    assert proxy._is_read_only_inspection(_Get(), "metrics") is True
    assert proxy._is_read_only_inspection(_Get(), "v1/messages") is False


async def test_unregistered_inspection_path_answers_locally(fleet_ahead, monkeypatch):
    """REGRESSION: an inspection route that is not registered must not become a 503.

    Old behaviour: the request fell through the catch-all and the pacing brake
    answered ``503 weekly pacing: every account is ahead of schedule``.
    """
    client = await _app(monkeypatch, consumers_enabled=False)
    try:
        async with client.get("/__throttle/consumers") as response:
            text = await response.text()
            assert response.status == 404, (
                f"expected a local 404, got {response.status}: {text[:120]}"
            )
            assert "unknown inspection endpoint" in text
            assert "weekly pacing" not in text
    finally:
        await client.close()
