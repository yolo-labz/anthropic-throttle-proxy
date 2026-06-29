"""End-to-end aiohttp test-client suite for the proxy hot path + control plane.

A stub upstream aiohttp server stands in for api.anthropic.com: it returns a
small SSE body with a ``usage`` block and ``anthropic-ratelimit-*`` headers on
200, a 429 with ``retry-after`` to drive the AIMD shrink path, and a 529
overload variant. We point ``config.UPSTREAM`` at the stub and drive the real
``handler`` / ``health`` / ``metrics`` / ``/ui`` routes through a TestClient,
covering the streaming, retry, disconnect-accounting, AIMD-feedback, usage-parse
and dashboard branches that the pure-function tests don't reach.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, limiter, pacing, proxy
from anthropic_throttle_proxy.ui.routes import _compute_status, attach_ui

# A minimal but realistic streamed Messages response: message_start carries the
# input usage, message_delta the output usage — exactly the two blocks the SSE
# usage parser sums.
_SSE_BODY = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":11,'
    b'"cache_read_input_tokens":3,"cache_creation_input_tokens":2}}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)

_RATELIMIT_HEADERS = {
    "anthropic-ratelimit-requests-remaining": "120",
    "anthropic-ratelimit-tokens-remaining": "55000",
    "content-type": "text/event-stream",
}


def _make_upstream() -> web.Application:
    """Build the stub upstream app. Behaviour is driven by request headers so a
    single server can return 200 / 429 / 529 / unified depending on the test."""

    seen_429_once = 0

    async def messages(request: web.Request) -> web.StreamResponse:
        nonlocal seen_429_once
        await request.read()
        mode = request.headers.get("X-Stub-Mode", "ok")
        if mode == "429":
            return web.Response(status=429, headers={"retry-after": "0", **_RATELIMIT_HEADERS})
        if mode == "429-long-retry-after":
            return web.Response(
                status=429,
                headers={"retry-after": "3600", **_RATELIMIT_HEADERS},
                text="rate limited",
            )
        if mode == "429-once":
            seen_429_once += 1
            if seen_429_once == 1:
                return web.Response(status=429, headers={"retry-after": "0", **_RATELIMIT_HEADERS})
        if mode == "529":
            return web.Response(status=529, headers={"retry-after": "0"})
        if mode in ("unified", "unified-high"):
            util_5h = "0.92" if mode == "unified-high" else "0.42"
            return web.Response(
                status=200,
                body=_SSE_BODY,
                headers={
                    "content-type": "text/event-stream",
                    "anthropic-ratelimit-unified-status": "allowed",
                    "anthropic-ratelimit-unified-5h-utilization": util_5h,
                    "anthropic-ratelimit-unified-7d-utilization": "0.1",
                },
            )
        resp = web.StreamResponse(status=200, headers=dict(_RATELIMIT_HEADERS))
        await resp.prepare(request)
        await resp.write(_SSE_BODY)
        await resp.write_eof()
        return resp

    async def passthrough(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(status=200, text="ok")

    app = web.Application()
    # Serve the SSE stub for any path containing "v1/messages" (incl. the
    # nested form used to exercise the usage-parse branch), passthrough else.
    app.router.add_route("*", "/{prefix:.*}v1/messages", messages)
    app.router.add_route("*", "/v1/messages", messages)
    app.router.add_route("*", "/{path:.*}", passthrough)
    return app


async def _post_and_settle(
    client: TestClient, path_suffix: str = "/v1/messages", **kwargs
) -> tuple[int, bytes]:
    """POST to ``path_suffix``, drain the streamed body, and let the handler's
    ``finally`` (AIMD feedback + usage parse) complete.

    With ``web.StreamResponse`` the client call resolves once headers arrive,
    but the proxy's per-request bookkeeping runs after ``write_eof`` in the
    server task. Yielding the loop a few times lets that task drain before the
    test inspects the shared state it mutated.
    """
    resp = await client.post(path_suffix, **kwargs)
    status = resp.status
    payload = await resp.read()
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    return status, payload


def _reset_proxy_state() -> None:
    """Clear the process-global registries so each test starts clean."""
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
            "central_consecutive_ok": 0,
            "central_consecutive_fail": 0,
            "last_advisor": None,
        }
    )


@pytest.fixture
async def client(monkeypatch) -> TestClient:
    """A TestClient wrapping the real proxy app, with locks bound + a live stub
    upstream. ``X-Stub-Mode`` request headers steer the upstream's response."""
    upstream_server = TestServer(_make_upstream())
    await upstream_server.start_server()
    upstream_url = str(upstream_server.make_url("")).rstrip("/")

    # Locks are normally bound in main(); bind them on the running loop here.
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "UPSTREAM", upstream_url)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    _reset_proxy_state()

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/", proxy.root_probe)
    app.router.add_get("/__throttle/health", proxy.health)
    app.router.add_get("/metrics", proxy.metrics)
    attach_ui(app)
    app.router.add_route("*", "/{path:.*}", proxy.handler)

    proxy_server = TestServer(app)
    test_client = TestClient(proxy_server)
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_health_fast_and_shaped(client: TestClient) -> None:
    resp = await client.get("/__throttle/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["max_concurrent"] == config.MAX_CONCURRENT
    assert body["upstream"] == config.UPSTREAM
    assert body["upstream_egress_ok"] is True
    assert body["upstream_egress_error"] == ""
    assert "bearers" in body
    assert body["central_status"] == "unknown"


async def test_health_returns_503_when_upstream_egress_probe_fails(
    client: TestClient, monkeypatch
) -> None:
    async def broken_egress() -> tuple[bool, str]:
        return False, "gaierror(-3)"

    monkeypatch.setattr(proxy, "_check_upstream_egress", broken_egress)

    resp = await client.get("/__throttle/health")
    assert resp.status == 503
    body = await resp.json()
    assert body["upstream_egress_ok"] is False
    assert body["upstream_egress_error"] == "gaierror(-3)"


async def test_metrics_endpoint_exposes_prometheus(client: TestClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    text = await resp.text()
    assert "anthropic_inflight" in text


async def test_start_time_gauge_registered(client: TestClient) -> None:
    # PR #037 observability: the start-time gauge is registered on the
    # process-local registry so a restart shows up as a step change in
    # /metrics. The gauge exists even before main() runs (which is what sets
    # the actual value), so its HELP line must appear in the scrape.
    text = await (await client.get("/metrics")).text()
    assert "anthropic_proxy_start_time_seconds" in text


def test_start_time_gauge_on_process_local_registry() -> None:
    # The gauge object lives on the isolated REGISTRY, never the global default.
    from prometheus_client import REGISTRY as _DEFAULT_GLOBAL

    from anthropic_throttle_proxy import metrics

    assert (
        "anthropic_proxy_start_time_seconds" in metrics.REGISTRY._names_to_collectors  # noqa: SLF001 — registry introspection in test
    )
    assert "anthropic_proxy_start_time_seconds" not in _DEFAULT_GLOBAL._names_to_collectors  # noqa: SLF001


async def test_root_probe_is_local_and_not_queued(client: TestClient) -> None:
    resp = await client.head("/")

    assert resp.status == 200
    assert config.state["queued"] == 0
    assert config.state["inflight"] == 0
    assert config.state["served"] == 0
    assert config.bearer_state == {}


async def test_post_messages_streams_and_mints_bearer(client: TestClient) -> None:
    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"claude-haiku-4-5","messages":[]}',
        headers={"Authorization": "Bearer test-oauth-abc"},
    )
    assert status == 200
    assert b"message_stop" in streamed
    # served counter advanced; a bearer slot was minted (8-hex anonymized id).
    assert config.state["served"] == 1
    assert len(config.bearer_state) == 1
    bid = next(iter(config.bearer_state))
    assert bid != "_anon"
    assert len(bid) == 8


async def test_usage_block_parsed_into_token_metrics(client: TestClient) -> None:
    # The usage-parse branch keys on '/v1/messages' appearing in the captured
    # path (match_info has no leading slash for a bare /v1/messages request, so
    # we drive the nested form that satisfies the production condition).
    status, _ = await _post_and_settle(
        client,
        path_suffix="/api/v1/messages",
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer usage-abc"},
    )
    assert status == 200
    metrics_text = await (await client.get("/metrics")).text()
    assert 'anthropic_tokens_total{kind="input"' in metrics_text
    assert 'anthropic_tokens_total{kind="output"' in metrics_text
    assert "anthropic_cost_usd_total" in metrics_text


async def test_bearer_token_never_appears_in_state(client: TestClient) -> None:
    fake_token = "Bearer super-secret-token-value-12345"  # noqa: S105 — test fixture, not a real credential
    await _post_and_settle(
        client, data=b'{"model":"claude-opus-4-7"}', headers={"Authorization": fake_token}
    )
    # The raw token must never be a bearer key (only its sha256[:8] digest).
    assert all("secret" not in bid for bid in config.bearer_state)
    blob = repr(config.state) + repr(list(config.bearer_state))
    assert "super-secret-token-value" not in blob


async def test_429_triggers_aimd_shrink(client: TestClient, monkeypatch) -> None:
    # observe/fair so AIMD counters move; default QUEUE_MODE is off → patch it.
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 5)
    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={"Authorization": "Bearer rate-limited", "X-Stub-Mode": "429"},
    )
    assert status == 429
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    # Live ceiling shrank below the hard ceiling after the 429 pushback.
    assert lim.max_concurrent < lim.hard_max
    assert lim._retry_after_until > time.time() + 4  # noqa: SLF001


async def test_429_without_retry_after_is_held_and_retried(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 1)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 0.01)
    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={"Authorization": "Bearer retry-after-zero", "X-Stub-Mode": "429-once"},
    )
    assert status == 200
    assert b"message_stop" in streamed
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    assert lim.max_concurrent < lim.hard_max
    assert config.state["served"] == 2


async def test_long_retry_after_fails_fast_without_sleeping(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 1)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 1.0)

    t0 = time.monotonic()
    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={
            "Authorization": "Bearer long-retry-after",
            "X-Stub-Mode": "429-long-retry-after",
        },
    )

    assert status == 429
    assert time.monotonic() - t0 < 1.0
    assert b"holding the local gateway request" in streamed
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    assert lim.retry_after_remaining() > 3500


async def test_active_long_retry_after_window_fails_fast_before_queue(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 1.0)
    bid = proxy._bearer_id({"authorization": "Bearer predispatch-long-retry"})
    lim = await proxy._get_bearer_limiter(bid, "fair", config.MAX_CONCURRENT)
    lim.note_retry_after(3600)

    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={"Authorization": "Bearer predispatch-long-retry"},
    )

    assert status == 429
    assert b"holding the local gateway request" in streamed
    assert config.state["queued"] == 0
    assert lim.snapshot()["queued_total"] == 0


async def test_529_overload_does_not_shrink(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-opus-4-7"}',
        headers={"Authorization": "Bearer overloaded", "X-Stub-Mode": "529"},
    )
    assert status == 529
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    # 529 = Anthropic-side overload → live cap is paused but not shrunk.
    assert lim.max_concurrent == 3
    assert lim.hard_max == config.MAX_CONCURRENT


async def test_unified_utilization_surfaced(client: TestClient) -> None:
    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-opus-4-7"}',
        headers={"Authorization": "Bearer oauth-unified", "X-Stub-Mode": "unified"},
    )
    assert status == 200
    bid = next(iter(config.bearer_state))
    assert config.bearer_state[bid]["unified"]["util_5h"] == 0.42


async def test_unified_proactive_shrink_fires_via_http(client: TestClient, monkeypatch) -> None:
    """FR-008 (Codex PARTIAL #3, PR #30): proactive util-shrink was only covered
    at the _apply_unified unit level. Drive it through the real HTTP path: a
    bearer whose binding 5h window (0.92) crosses UTILIZATION_TARGET (0.85) must
    have its live cap shrunk one CUBIC step below the hard ceiling."""
    # observe/fair so the AIMD ceiling can move (shrink is a no-op in off mode).
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.85)
    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-opus-4-7"}',
        headers={"Authorization": "Bearer oauth-util-high", "X-Stub-Mode": "unified-high"},
    )
    assert status == 200
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    assert config.bearer_state[bid]["unified"]["util_5h"] == 0.92
    assert lim.max_concurrent < lim.hard_max


async def test_unified_no_shrink_when_target_off_via_http(client: TestClient, monkeypatch) -> None:
    """Mirror of the above with UTILIZATION_TARGET=0 (default): high utilization
    is surfaced but the live cap is untouched — observe-only, no proactive shrink.
    Same observe mode as the positive case, so the only variable is the target."""
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    assert proxy.UTILIZATION_TARGET == 0
    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-opus-4-7"}',
        headers={"Authorization": "Bearer oauth-util-off", "X-Stub-Mode": "unified-high"},
    )
    assert status == 200
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    assert config.bearer_state[bid]["unified"]["util_5h"] == 0.92
    assert lim.max_concurrent == config.AIMD_INITIAL_CONCURRENT
    assert lim.hard_max == config.MAX_CONCURRENT


async def test_health_reflects_inflight_after_traffic(client: TestClient) -> None:
    await _post_and_settle(
        client, data=b'{"model":"claude-haiku-4-5"}', headers={"Authorization": "Bearer h"}
    )
    body = await (await client.get("/__throttle/health")).json()
    assert body["served"] >= 1
    assert body["inflight"] == 0  # settled after the request completed


async def test_ui_dashboard_renders(client: TestClient) -> None:
    resp = await client.get("/ui")
    assert resp.status == 200
    html = await resp.text()
    assert "<table" in html.lower() or "html" in html.lower()


async def test_ui_stats_partial_renders(client: TestClient) -> None:
    resp = await client.get("/ui/stats")
    assert resp.status == 200


async def test_ui_advisor_disabled_renders_inline_error(client: TestClient, monkeypatch) -> None:
    """Disabled advisor returns 200 with an HTML error partial so HTMX swaps
    it into #advisor-out instead of silently dropping the response on
    non-2xx. Pedro reported 27/05/2026 the integration looked broken; the
    cause was the prior 503 never surfacing in the dashboard."""
    monkeypatch.delenv("ADVISOR_ENABLED", raising=False)
    resp = await client.post("/ui/advisor")
    assert resp.status == 200
    body = await resp.text()
    assert "advisor-output err" in body
    assert "ADVISOR_ENABLED" in body


async def test_get_passthrough_non_messages_path(client: TestClient) -> None:
    resp = await client.get("/v1/models", headers={"Authorization": "Bearer m"})
    assert resp.status == 200
    assert await resp.text() == "ok"


async def test_fair_mode_queues_and_serves(client: TestClient, monkeypatch) -> None:
    # In fair mode the request is enqueued (queued counter bumps) then dispatched.
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer fair-client"},
    )
    assert status == 200
    body = await (await client.get("/__throttle/health")).json()
    assert body["served"] >= 1
    assert body["queued"] == 0  # drained after dispatch


# ── _compute_status (pure view-layer verdict) ──────────────────────────────
# Drives the dashboard status strip; pure function so we test the worst-wins
# precedence and the "binding" line directly, without standing up the app.


def _bearer(bid: str, *, util=None, status="allowed", retry=None, live=8, hard=8, queued=0):
    """Build a minimal bearer view dict shaped like ``_collect_view`` emits."""
    return {
        "bearer_id": bid,
        "queued": queued,
        "unified": None if util is None else {"util_5h": util, "status": status},
        "last_ratelimit": None if retry is None else {"retry-after": retry},
        "limiter": {"max_concurrent": live, "hard_max": hard},
    }


def test_compute_status_idle_when_no_bearers() -> None:
    out = _compute_status([], "fair")
    assert out["level"] == "idle"
    assert out["verdict"] == "IDLE"


def test_compute_status_healthy_clear() -> None:
    out = _compute_status([_bearer("aa", util=0.30)], "fair")
    assert out["level"] == "healthy"
    assert out["verdict"] == "HEALTHY"
    # binding line names the (only) bearer + its window.
    assert "30% on aa" in out["detail"]


def test_compute_status_pacing_on_high_utilization() -> None:
    out = _compute_status([_bearer("aa", util=0.85)], "fair")
    assert out["level"] == "pacing"
    assert "1 of 1 bearer pacing" in out["detail"]


def test_compute_status_pacing_on_shrunk_ceiling() -> None:
    # live < hard (AIMD shrank) counts as pacing even at low utilization.
    out = _compute_status([_bearer("aa", util=0.10, live=4, hard=8)], "fair")
    assert out["level"] == "pacing"


def test_compute_status_throttled_wins_over_pacing() -> None:
    bearers = [
        _bearer("pace", util=0.82),  # pacing
        _bearer("rej", util=0.97, status="rejected"),  # throttled
    ]
    out = _compute_status(bearers, "fair")
    assert out["level"] == "throttled"
    assert "1 of 2 bearers throttled" in out["detail"]
    # binding = highest-utilization bearer.
    assert "97% on rej" in out["detail"]


def test_compute_status_retry_after_throttles_and_annotates() -> None:
    out = _compute_status([_bearer("aa", util=0.50, retry="38")], "fair")
    assert out["level"] == "throttled"
    assert "retry-after 38" in out["detail"]


def test_compute_status_notes_passthrough_when_queue_off() -> None:
    out = _compute_status([_bearer("aa", util=0.20)], "off")
    assert out["detail"].endswith("queue off (passthrough)")


# ---------------------------------------------------------------------------
# PR #043: client-disconnect attribution
# ---------------------------------------------------------------------------


def test_record_disconnect_logs_request_context(monkeypatch) -> None:
    _reset_proxy_state()
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)

    attempt = proxy._Attempt()
    attempt.started_at = time.time() - 1
    attempt.context = {
        "method": "POST",
        "bid": "48be3268",
        "cid": "127.0.0.1:57112",
        "via": "central",
        "model": "claude-opus-4-8",
    }

    response = proxy._record_disconnect(
        "v1/messages",
        "first",
        aiohttp.ClientConnectionResetError("Cannot write to closing transport"),
        attempt,
    )

    assert response.status == 499
    assert attempt.final_status == 499
    assert config.state["client_disconnects"] == 1
    assert len(lines) == 1
    line = lines[0]
    assert "where=first" in line
    assert "path=/v1/messages" in line
    assert "method=POST" in line
    assert "bid=48be3268" in line
    assert "cid=127.0.0.1:57112" in line
    assert "via=central" in line
    assert "model=claude-opus-4-8" in line
    assert "elapsed_ms=" in line
    assert "ClientConnectionResetError" in line
    assert "no_upstream_retry=true" in line


def _assert_single_disconnect_without_upstream() -> None:
    assert config.state["client_disconnects"] == 1
    assert config.state["upstream_retries"] == 0
    assert config.state["served"] == 0


async def test_handler_drops_client_disconnects_before_upstream(
    client: TestClient, monkeypatch
) -> None:
    payload = b'{"model":"claude-opus-4-8"}'

    with monkeypatch.context() as patch:

        async def raise_reset(_request: web.Request) -> bytes:
            raise ConnectionResetError("reset during upload")

        patch.setattr(web.Request, "read", raise_reset)
        status, _ = await _post_and_settle(
            client,
            data=payload,
            headers={"Authorization": "Bearer upload-reset"},
        )
        assert status == 499
        _assert_single_disconnect_without_upstream()

    _reset_proxy_state()
    with monkeypatch.context() as patch:
        checks = iter([False, True])

        def fake_disconnected(_request: web.Request) -> bool:
            return next(checks, True)

        async def fail_forward(*_args, **_kwargs):  # pragma: no cover - should not run
            raise AssertionError("disconnected request reached upstream forwarding")

        patch.setattr(proxy, "_request_disconnected", fake_disconnected)
        patch.setattr(proxy, "_forward_with_retry", fail_forward)
        status, _ = await _post_and_settle(
            client,
            data=payload,
            headers={"Authorization": "Bearer closed-before-forward"},
        )
        assert status == 499
        _assert_single_disconnect_without_upstream()
        assert config.state["inflight"] == 0
        assert config.state["queued"] == 0


# ---------------------------------------------------------------------------
# PR #037: storm early-warning latch (proxy._maybe_warn_storm)
# ---------------------------------------------------------------------------


@pytest.fixture
def storm_log(monkeypatch) -> list[str]:
    """Capture proxy.log lines and reset the storm latch around each test."""
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    monkeypatch.setattr(proxy, "_storm_warned", False)
    monkeypatch.setattr(config, "STORM_WARN_RETRIES", 25)
    yield lines
    proxy._storm_warned = False


def test_storm_warn_fires_once_on_crossing(storm_log: list[str]) -> None:
    proxy._maybe_warn_storm(24)  # below threshold — silent
    assert storm_log == []
    proxy._maybe_warn_storm(25)  # crosses — one warning
    proxy._maybe_warn_storm(40)  # still over — no repeat
    storms = [m for m in storm_log if "STORM WARNING" in m]
    assert len(storms) == 1
    assert "retries=25" in storms[0]
    assert "THROTTLE_STORM_WARN_RETRIES=25" in storms[0]


def test_storm_warn_rearms_after_drop_below(storm_log: list[str]) -> None:
    proxy._maybe_warn_storm(30)  # warn
    proxy._maybe_warn_storm(2)  # counter fell back (fresh process / reset)
    proxy._maybe_warn_storm(30)  # warn again on the next crossing
    assert len([m for m in storm_log if "STORM WARNING" in m]) == 2


def test_storm_warn_disabled_when_threshold_non_positive(storm_log: list[str], monkeypatch) -> None:
    monkeypatch.setattr(config, "STORM_WARN_RETRIES", 0)
    proxy._maybe_warn_storm(1000)
    assert [m for m in storm_log if "STORM WARNING" in m] == []
