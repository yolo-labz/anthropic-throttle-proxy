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
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import accounts, config, limiter, pacing, proxy
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
_UNIFIED_ALLOWED_LOW_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.19",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.23",
}
_UNIFIED_REJECTED_HEADERS = {
    "anthropic-ratelimit-unified-status": "rejected",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-5h-status": "rejected",
    "anthropic-ratelimit-unified-5h-utilization": "1.0",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.5",
}


def _make_upstream() -> web.Application:
    """Build the stub upstream app. Behaviour is driven by request headers so a
    single server can return 200 / 429 / 529 / unified depending on the test."""

    seen_429_once = 0
    probe_state = {
        "attempts": {},
        "b_started": asyncio.Event(),
        "release_b_headers": asyncio.Event(),
        "slow_headers": asyncio.Event(),
        "release_slow_eof": asyncio.Event(),
    }

    async def messages(request: web.Request) -> web.StreamResponse:
        nonlocal seen_429_once
        await request.read()
        mode = request.headers.get("X-Stub-Mode", "ok")
        authorization = request.headers.get("Authorization", "")
        attempts = probe_state["attempts"]
        attempts[authorization] = attempts.get(authorization, 0) + 1
        attempt_number = attempts[authorization]
        if mode == "probe-block-headers" and authorization.endswith("PROBE-B"):
            probe_state["b_started"].set()
            await probe_state["release_b_headers"].wait()
        if mode == "probe-slow-eof" and attempt_number == 1:
            response = web.StreamResponse(status=200, headers=_RATELIMIT_HEADERS)
            await response.prepare(request)
            probe_state["slow_headers"].set()
            await probe_state["release_slow_eof"].wait()
            await response.write(_SSE_BODY)
            await response.write_eof()
            return response
        if mode == "429":
            return web.Response(status=429, headers={"retry-after": "0", **_RATELIMIT_HEADERS})
        if mode == "429-long-retry-after":
            return web.Response(
                status=429,
                headers={"retry-after": "3600", **_RATELIMIT_HEADERS},
                text="rate limited",
            )
        if mode == "zai-quota-1316":
            return web.json_response(
                {
                    "error": {
                        "code": "1316",
                        "message": "5h plan quota exhausted",
                        "reset_time": int(time.time() + 3600),
                    }
                },
                status=429,
            )
        if mode == "zai-concurrency-1302":
            return web.json_response(
                {"error": {"code": "1302", "message": "too many concurrent requests"}},
                status=429,
            )
        if mode == "429-once":
            seen_429_once += 1
            if seen_429_once == 1:
                return web.Response(status=429, headers={"retry-after": "0", **_RATELIMIT_HEADERS})
        if mode == "429-unified-low-once":
            seen_429_once += 1
            if seen_429_once == 1:
                return web.Response(status=429, headers=_UNIFIED_ALLOWED_LOW_HEADERS)
        if mode == "429-unified-low":
            # Concurrency/per-rate 429: every window allowed, low util, no
            # Retry-After — the transient burst that must NOT collapse the cap.
            return web.Response(status=429, headers=_UNIFIED_ALLOWED_LOW_HEADERS)
        if mode == "429-unified-rejected":
            # Budget 429: a window already "rejected" — real exhaustion that
            # must still multiplicatively decrease the cap.
            return web.Response(status=429, headers=_UNIFIED_REJECTED_HEADERS)
        if mode == "529":
            return web.Response(status=529, headers={"retry-after": "0"})
        if mode == "400":
            authorization = request.headers.get("Authorization", "missing")
            return web.json_response(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "message": (
                            "max_tokens must be greater than zero; "
                            f"Authorization: {authorization}; x-api-key=gsk_reflected_test_secret"
                        ),
                    }
                },
                status=400,
            )
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
        if mode == "spoof-queue-timeout":
            # A raw upstream 503 asserting the proxy-private queue-timeout
            # header (no proxy marker) — must be stripped and still shrink.
            return web.Response(
                status=503,
                headers={"retry-after": "0", config.QUEUE_TIMEOUT_HEADER: "1"},
                text="spoofed",
            )
        ok_headers = dict(_RATELIMIT_HEADERS)
        # Echo EVERY received budget value (case-insensitive) so tests can
        # detect a duplicated client copy sneaking past the canonical stamp.
        budget_seen = request.headers.getall(config.WAIT_BUDGET_HEADER, [])
        if budget_seen:
            ok_headers["x-echo-wait-budget"] = ",".join(budget_seen)
        resp = web.StreamResponse(status=200, headers=ok_headers)
        await resp.prepare(request)
        await resp.write(_SSE_BODY)
        await resp.write_eof()
        return resp

    async def passthrough(request: web.Request) -> web.Response:
        await request.read()
        authorization = request.headers.get("Authorization", "")
        if authorization.endswith("usage-budget-locked"):
            reset = str(int(time.time()) + 3600)
            return web.Response(
                status=429,
                headers={
                    "retry-after": "3600",
                    **_UNIFIED_REJECTED_HEADERS,
                    "anthropic-ratelimit-unified-reset": reset,
                    "anthropic-ratelimit-unified-5h-reset": reset,
                },
                text="budget locked",
            )
        if authorization.endswith("usage-poll-limited"):
            return web.Response(
                status=429,
                headers={"retry-after": "3600"},
                text="usage endpoint rate limited",
            )
        if request.headers.get("X-Stub-Mode") == "oauth-usage-429":
            reset = str(int(time.time()) + 3600)
            return web.Response(
                status=429,
                headers={
                    "retry-after": "3600",
                    **_UNIFIED_REJECTED_HEADERS,
                    "anthropic-ratelimit-unified-reset": reset,
                    "anthropic-ratelimit-unified-5h-reset": reset,
                },
                text="rate limited",
            )
        return web.Response(status=200, text="ok")

    app = web.Application()
    # Serve the SSE stub for any path containing "v1/messages" (incl. the
    # nested form used to exercise the usage-parse branch), passthrough else.
    app.router.add_route("*", "/{prefix:.*}v1/messages", messages)
    app.router.add_route("*", "/v1/messages", messages)
    app.router.add_route("*", "/{path:.*}", passthrough)
    app.probe_state = probe_state
    return app


async def _request_and_settle(
    client: TestClient, method: str = "POST", path_suffix: str = "/v1/messages", **kwargs
) -> tuple[int, bytes]:
    """Request ``path_suffix``, drain the streamed body, and let the handler's
    ``finally`` (AIMD feedback + usage parse) complete.

    With ``web.StreamResponse`` the client call resolves once headers arrive,
    but the proxy's per-request bookkeeping runs after ``write_eof`` in the
    server task. Yielding the loop a few times lets that task drain before the
    test inspects the shared state it mutated.
    """
    resp = await client.request(method, path_suffix, **kwargs)
    status = resp.status
    payload = await resp.read()
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)
    return status, payload


async def _post_and_settle(
    client: TestClient, path_suffix: str = "/v1/messages", **kwargs
) -> tuple[int, bytes]:
    return await _request_and_settle(client, path_suffix=path_suffix, **kwargs)


async def _wait_for_limiter_queued(lim, expected: int) -> None:
    for _ in range(50):
        if lim.snapshot()["queued_total"] == expected:
            return
        await asyncio.sleep(0.01)


def _force_single_slot_fair_queue(monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 1)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 1.0)


def _reset_proxy_state() -> None:
    """Clear the process-global registries so each test starts clean."""
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    accounts._endpoint_backoff.clear()
    accounts._endpoint_locks.clear()
    accounts._endpoint_cache_loaded = False
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
            "upstream_egress_ok": True,
            "upstream_egress_error": "",
            "upstream_egress_last_check": 0,
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
    test_client.probe_state = upstream_server.app.probe_state
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
    await proxy._refresh_upstream_egress()

    resp = await client.get("/__throttle/health")
    assert resp.status == 503
    body = await resp.json()
    assert body["upstream_egress_ok"] is False
    assert body["upstream_egress_error"] == "gaierror(-3)"


async def test_health_does_not_wait_for_slow_upstream_probe(
    client: TestClient, monkeypatch
) -> None:
    release = asyncio.Event()

    async def slow_egress() -> tuple[bool, str]:
        await release.wait()
        return True, ""

    monkeypatch.setattr(proxy, "_check_upstream_egress", slow_egress)
    refresh = asyncio.create_task(proxy._refresh_upstream_egress())
    try:
        resp = await asyncio.wait_for(client.get("/__throttle/health"), timeout=0.05)
        assert resp.status == 200
    finally:
        release.set()
        await refresh


async def test_upstream_probe_timeout_becomes_unhealthy(monkeypatch) -> None:
    async def timed_out_egress() -> tuple[bool, str]:
        return False, "TimeoutError: "

    monkeypatch.setattr(proxy, "_check_upstream_egress", timed_out_egress)
    config.state["upstream_egress_ok"] = True

    await proxy._refresh_upstream_egress()

    assert config.state["upstream_egress_ok"] is False
    assert config.state["upstream_egress_error"] == "TimeoutError: "
    assert float(config.state["upstream_egress_last_check"]) > 0


async def test_upstream_egress_context_cancels_probe_task(monkeypatch) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def waiting_loop() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(proxy, "_upstream_egress_loop", waiting_loop)
    context = proxy._upstream_egress_context(web.Application())

    await anext(context)
    await asyncio.wait_for(started.wait(), timeout=0.05)
    with pytest.raises(StopAsyncIteration):
        await anext(context)

    assert stopped.is_set()


async def test_upstream_egress_loop_recovers_after_exception(monkeypatch) -> None:
    calls = 0
    survived = asyncio.Event()
    lines: list[str] = []
    real_sleep = asyncio.sleep

    async def flaky_refresh() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("probe exploded")
        config.state["upstream_egress_ok"] = True
        survived.set()

    async def fast_sleep(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(proxy, "_refresh_upstream_egress", flaky_refresh)
    monkeypatch.setattr(proxy.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(proxy, "log", lines.append)
    task = asyncio.create_task(proxy._upstream_egress_loop())

    await asyncio.wait_for(survived.wait(), timeout=0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert calls >= 2
    assert lines == ["upstream egress probe error: RuntimeError('probe exploded')"]


async def test_upstream_egress_loop_retries_failures_quickly(monkeypatch) -> None:
    delays: list[float] = []

    async def refresh() -> None:
        config.state["upstream_egress_ok"] = bool(delays)

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(config, "UPSTREAM_HEALTH_INTERVAL", 30.0)
    monkeypatch.setattr(proxy, "_refresh_upstream_egress", refresh)
    monkeypatch.setattr(proxy.asyncio, "sleep", capture_sleep)

    with pytest.raises(asyncio.CancelledError):
        await proxy._upstream_egress_loop()

    assert delays == [5.0, 30.0]


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


async def test_expiry_burst_makes_exactly_one_half_open_b_attempt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cred_a = tmp_path / "a.json"
    cred_b = tmp_path / "b.json"
    raw_a = "sk-ant-oat01-PROBE-A"
    raw_b = "sk-ant-oat01-PROBE-B"
    cred_a.write_text(json.dumps({"claudeAiOauth": {"accessToken": raw_a}}))
    cred_b.write_text(json.dumps({"claudeAiOauth": {"accessToken": raw_b}}))
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred_a},B:{cred_b}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "budget_paced")
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(tmp_path / "retry-after.json"))
    monkeypatch.setattr(limiter, "_retry_after_state", None)

    bid_a = proxy._bearer_id({"Authorization": f"Bearer {raw_a}"})
    bid_b = proxy._bearer_id({"Authorization": f"Bearer {raw_b}"})
    lim_a = await proxy._get_bearer_limiter(bid_a, "off", 6)
    assert lim_a.try_begin_retry_probe() is True
    assert lim_a.finish_retry_probe(success=True) is True
    lim_a.inflight = 6
    config.bearer_state[bid_a]["unified"] = {
        "status": "allowed_warning",
        "status_5h": "allowed",
        "status_7d": "allowed_warning",
        "util_5h": 0.39,
        "util_7d": 0.78,
    }
    lim_b = await proxy._get_bearer_limiter(bid_b, "off", 6)
    lim_b.note_retry_after(116_212)
    monkeypatch.setattr(lim_b, "_retry_after_until", time.time() - 1)

    probe_state = client.probe_state
    headers = {
        "Authorization": f"Bearer {raw_a}",
        "X-Stub-Mode": "probe-block-headers",
    }
    body = {"model": "claude-opus-4-8", "max_tokens": 1024, "messages": []}
    requests = [
        asyncio.create_task(client.post("/v1/messages", headers=headers, json=body))
        for _ in range(9)
    ]
    await asyncio.wait_for(probe_state["b_started"].wait(), timeout=1)
    await asyncio.sleep(0.05)

    attempts = probe_state["attempts"]
    assert attempts[f"Bearer {raw_b}"] == 1
    probe_state["release_b_headers"].set()
    responses = await asyncio.gather(*requests)
    await asyncio.gather(*(response.read() for response in responses))

    assert {response.status for response in responses} == {200}
    assert attempts == {f"Bearer {raw_a}": 8, f"Bearer {raw_b}": 1}


async def test_success_headers_open_probe_before_slow_stream_eof(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cred_b = tmp_path / "b.json"
    raw_b = "sk-ant-oat01-SLOW-PROBE-B"
    cred_b.write_text(json.dumps({"claudeAiOauth": {"accessToken": raw_b}}))
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"B:{cred_b}")
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(tmp_path / "retry-after.json"))
    monkeypatch.setattr(limiter, "_retry_after_state", None)

    bid_b = proxy._bearer_id({"Authorization": f"Bearer {raw_b}"})
    lim_b = await proxy._get_bearer_limiter(bid_b, "off", 6)
    lim_b.note_retry_after(3600)
    monkeypatch.setattr(lim_b, "_retry_after_until", time.time() - 1)
    probe_state = client.probe_state
    headers = {
        "Authorization": f"Bearer {raw_b}",
        "X-Stub-Mode": "probe-slow-eof",
    }
    body = {"model": "claude-opus-4-8", "max_tokens": 1024, "messages": []}

    first_request = asyncio.create_task(client.post("/v1/messages", headers=headers, json=body))
    await asyncio.wait_for(probe_state["slow_headers"].wait(), timeout=1)
    first_response = await asyncio.wait_for(first_request, timeout=1)
    assert lim_b.retry_probe_required() is False
    assert probe_state["release_slow_eof"].is_set() is False

    second_response = await asyncio.wait_for(
        client.post("/v1/messages", headers=headers, json=body), timeout=1
    )
    await asyncio.wait_for(second_response.read(), timeout=1)
    assert second_response.status == 200
    assert probe_state["attempts"][f"Bearer {raw_b}"] == 2

    probe_state["release_slow_eof"].set()
    await asyncio.wait_for(first_response.read(), timeout=1)


def _tiny_probe_payload(content: object = "") -> dict[str, object]:
    return {
        "model": "claude-opus-4-8",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": content}],
    }


def _tiny_probe_body(content: object = "", **overrides: object) -> bytes:
    payload = _tiny_probe_payload(content)
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode()


def _assert_forwarded(status: int, streamed: bytes) -> None:
    assert status == 200
    assert b"message_stop" in streamed
    assert config.state["served"] == 1


async def test_synthetic_one_token_probe_answers_claude_cli_locally(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES", "1")

    status, payload = await _post_and_settle(
        client,
        data=_tiny_probe_body(""),
        headers={"Authorization": "Bearer cli-probe", "User-Agent": "claude-cli/1.2.3"},
    )

    body = json.loads(payload)
    assert status == 200
    assert body["id"].startswith("msg_throttle_probe_")
    assert body["content"] == [{"type": "text", "text": "."}]
    assert config.state["served"] == 0
    assert config.bearer_state == {}


@pytest.mark.parametrize(
    ("body", "headers", "token"),
    [
        (_tiny_probe_body("OK?"), {"User-Agent": "claude-cli/1.2.3"}, "real-short-text"),
        (
            _tiny_probe_body([{"type": "image", "text": "OK?", "source": {}}]),
            {"User-Agent": "claude-cli/1.2.3"},
            "non-text-block",
        ),
        (_tiny_probe_body(), {"User-Agent": "codex-cli/0.1"}, "codex-probe"),
        (_tiny_probe_body(max_tokens=2), {"User-Agent": "claude-cli/1.2.3"}, "max-tokens"),
        (
            _tiny_probe_body(tools=[{"name": "noop", "input_schema": {"type": "object"}}]),
            {"User-Agent": "claude-cli/1.2.3"},
            "tools",
        ),
        (
            _tiny_probe_body(tool_choice={"type": "auto"}),
            {"User-Agent": "claude-cli/1.2.3"},
            "tool-choice",
        ),
        (_tiny_probe_body(stream=True), {"User-Agent": "claude-cli/1.2.3"}, "stream"),
        (
            _tiny_probe_body(system="answer this system prompt"),
            {"User-Agent": "claude-cli/1.2.3"},
            "system",
        ),
        (
            _tiny_probe_body("x" * 2100),
            {"User-Agent": "claude-cli/1.2.3"},
            "oversize",
        ),
        (
            _tiny_probe_body(
                messages=[
                    {"role": "user", "content": ""},
                    {"role": "assistant", "content": ""},
                    {"role": "user", "content": ""},
                ]
            ),
            {"User-Agent": "claude-cli/1.2.3"},
            "too-many-messages",
        ),
    ],
)
async def test_synthetic_probe_forwards_negative_shapes(
    client: TestClient, monkeypatch, body: bytes, headers: dict[str, str], token: str
) -> None:
    monkeypatch.setenv("THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES", "1")

    status, streamed = await _post_and_settle(
        client,
        data=body,
        headers={"Authorization": f"Bearer {token}", **headers},
    )

    _assert_forwarded(status, streamed)


async def test_synthetic_probe_forwards_when_env_disabled(client: TestClient) -> None:
    status, streamed = await _post_and_settle(
        client,
        data=_tiny_probe_body(),
        headers={"Authorization": "Bearer env-disabled", "User-Agent": "claude-cli/1.2.3"},
    )

    _assert_forwarded(status, streamed)


@pytest.mark.parametrize(
    ("method", "path_suffix"), [("GET", "/v1/messages"), ("POST", "/api/v1/messages")]
)
async def test_synthetic_probe_forwards_wrong_method_or_path(
    client: TestClient, monkeypatch, method: str, path_suffix: str
) -> None:
    monkeypatch.setenv("THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES", "1")

    status, streamed = await _request_and_settle(
        client,
        method=method,
        path_suffix=path_suffix,
        data=_tiny_probe_body(),
        headers={
            "Authorization": f"Bearer {method}-{path_suffix}",
            "User-Agent": "claude-cli/1.2.3",
        },
    )

    _assert_forwarded(status, streamed)


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


async def test_concurrency_429_retry_uses_classified_cooldown(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 1)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 5.0)
    monkeypatch.setattr(config, "CONCURRENCY_COOLDOWN_S", 0.01)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 1.0)

    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={
            "Authorization": "Bearer concurrency-retry",
            "X-Stub-Mode": "429-unified-low-once",
        },
    )

    assert status == 200
    assert b"message_stop" in streamed
    assert config.state["served"] == 2


async def test_concurrency_429_burst_does_not_collapse_aimd_cap(
    client: TestClient, monkeypatch
) -> None:
    # Regression (17/07 19:03): three headerless concurrency 429s (windows
    # allowed, low util) on an active account shrank the cap 8→4→2→1 → queue
    # flood → queue_max_wait 503 storm → 3 dead claude sessions. A concurrency
    # 429 is a transient inflight spike; the short cooldown paces it and the cap
    # MUST stay at hard_max. Shrink is reserved for real budget/Retry-After.
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    monkeypatch.setattr(config, "CONCURRENCY_COOLDOWN_S", 0.0)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    for _ in range(3):
        status, _ = await _post_and_settle(
            client,
            data=b'{"model":"claude-sonnet-4-6"}',
            headers={
                "Authorization": "Bearer conc-burst",
                "X-Stub-Mode": "429-unified-low",
            },
        )
        assert status == 429
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    # Cap unchanged from its AIMD_INITIAL_CONCURRENT start despite three
    # concurrency 429s — the multiplicative-decrease that collapsed 8→1 is gone.
    assert lim.max_concurrent == config.AIMD_INITIAL_CONCURRENT


async def test_budget_rejected_429_still_shrinks(client: TestClient, monkeypatch) -> None:
    # Guard against over-broadening the concurrency carve-out: a 429 whose
    # unified window is already "rejected" is real budget exhaustion and must
    # still multiplicatively decrease the cap.
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={
            "Authorization": "Bearer budget-rejected",
            "X-Stub-Mode": "429-unified-rejected",
        },
    )
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    # Started at AIMD_INITIAL_CONCURRENT=3; a rejected-window 429 must shrink it.
    assert lim.max_concurrent < config.AIMD_INITIAL_CONCURRENT


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


async def test_zai_quota_gate_pauses_without_aimd_shrink(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 1)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 1.0)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    monkeypatch.setattr(config, "ZAI_QUOTA_RESET_JITTER_S", 0.0)

    t0 = time.monotonic()
    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"glm-5.2"}',
        headers={
            "Authorization": "Bearer zai-quota",
            "X-Stub-Mode": "zai-quota-1316",
        },
    )

    assert status == 429
    assert time.monotonic() - t0 < 1.0
    assert b"holding the local gateway request" in streamed
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    assert lim.max_concurrent == 3
    assert lim.retry_after_remaining() > 3500
    meta = config.bearer_state[bid]["last_ratelimit"]
    assert meta["zai-error-code"] == "1316"
    assert meta["zai-quota-gate"] == "true"


async def test_zai_concurrency_1302_still_triggers_aimd_shrink(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 5)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)

    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"glm-5.2"}',
        headers={
            "Authorization": "Bearer zai-concurrency",
            "X-Stub-Mode": "zai-concurrency-1302",
        },
    )

    assert status == 429
    bid = next(iter(config.bearer_state))
    lim = config.bearer_limiters[bid]
    assert lim.max_concurrent < 3
    assert config.bearer_state[bid]["last_ratelimit"]["zai-error-code"] == "1302"


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


async def test_retry_after_window_does_not_block_oauth_probe(
    client: TestClient, monkeypatch
) -> None:
    _force_single_slot_fair_queue(monkeypatch)
    bid = proxy._bearer_id({"authorization": "Bearer oauth-probe-retry"})
    lim = await proxy._get_bearer_limiter(bid, "fair", config.MAX_CONCURRENT)
    lim.note_retry_after(3600)

    resp = await client.get(
        "/api/oauth/profile",
        headers={"Authorization": "Bearer oauth-probe-retry"},
    )
    body = await resp.text()
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    assert resp.status == 200
    assert body == "ok"
    assert config.state["queued"] == 0
    assert config.state["inflight"] == 0
    assert lim.snapshot()["queued_total"] == 0


async def test_oauth_usage_429_does_not_poison_message_limiter(
    client: TestClient, monkeypatch
) -> None:
    # A 429 from /api/oauth/usage is the telemetry endpoint's own rate limit,
    # not a POST /v1/messages quota throttle. It must not AIMD-shrink the
    # bearer nor set a retry-after pause — otherwise each dashboard poll
    # collapses the workhorse for ~58 min (13/07 incident).
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 1)
    monkeypatch.setattr(config, "MAX_CONCURRENT", 3)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 1.0)
    advisor_calls: list[tuple[str, int]] = []

    async def fake_advise(trigger_bid: str, trigger_status: int) -> None:
        advisor_calls.append((trigger_bid, trigger_status))

    monkeypatch.setattr(proxy, "ADVISOR_ENABLED", True)
    monkeypatch.setattr(proxy, "_maybe_advise", fake_advise)
    bid = proxy._bearer_id({"authorization": "Bearer usage-poll"})

    resp = await client.get(
        "/api/oauth/usage",
        data=b'{"stream":true}',
        headers={"Authorization": "Bearer usage-poll", "X-Stub-Mode": "oauth-usage-429"},
    )
    await resp.read()
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    # The 429 is relayed unchanged; the poller backs off its own Retry-After.
    assert resp.status == 429
    lim = config.bearer_limiters[bid]
    # No AIMD shrink, no retry-after pause — bearer stays fully usable for
    # real /v1/messages traffic.
    assert lim.max_concurrent == 3
    assert lim.retry_after_remaining() == 0.0
    assert advisor_calls == []
    assert config.bearer_state[bid]["last_ratelimit"] is None


async def test_usage_poller_marks_only_budget_rejected_429_as_locked(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """Telemetry lock evidence stays display-only and budget-classified.

    Both responses carry the same long Retry-After. Only the 429 whose same
    response says the unified budget is rejected may render as account-locked;
    the usage endpoint's own rate limit must only back its poller off. Neither
    response may contaminate message AIMD or message Retry-After state.
    """
    budget_token = "usage-" + "budget-locked"
    poll_token = "usage-" + "poll-limited"
    now = time.time()
    creds: list[tuple[str, object, str]] = []
    for label, token in (("A", budget_token), ("B", poll_token)):
        path = tmp_path / f"{label.lower()}.json"
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": token,
                        "expiresAt": int((now + 3600) * 1000),
                    }
                }
            )
        )
        creds.append((label, path, token))

    monkeypatch.setattr(
        config, "ACCOUNT_CRED_PATHS", ",".join(f"{label}:{path}" for label, path, _ in creds)
    )
    monkeypatch.setattr(config, "ENDPOINT_CACHE_FILE", tmp_path / "endpoint-cache.json")
    monkeypatch.setattr(accounts, "_oauth_base", lambda: str(client.make_url("/")).rstrip("/"))

    endpoint = await accounts.refresh_endpoint(now)
    bearers = [{"bearer_id": bid, **state} for bid, state in config.bearer_state.items()]
    view = {row["label"]: row for row in accounts.account_view(bearers, now, endpoint)}

    assert view["A"]["locked_in"] == accounts._fmt_duration(3600)
    assert view["B"]["locked_in"] is None
    for _label, _path, token in creds:
        bid = proxy._bearer_id({"Authorization": f"Bearer {token}"})
        # The real loopback GET traverses proxy.handler, which creates neutral
        # per-bearer state; telemetry isolation means that state stays neutral.
        lim = config.bearer_limiters[bid]
        assert lim.max_concurrent == config.AIMD_INITIAL_CONCURRENT
        assert lim.retry_after_remaining() == 0.0
        assert config.bearer_state[bid]["last_ratelimit"] is None


async def test_upstream_400_log_identifies_request_without_bearer_secret(
    client: TestClient, monkeypatch
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)
    bearer = "Bearer secret-value-that-must-not-be-logged"

    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-opus-4-8","max_tokens":0}',
        headers={"Authorization": bearer, "X-Stub-Mode": "400"},
    )

    assert status == 400
    reason = next(line for line in lines if line.startswith("upstream_400 "))
    assert "path=/v1/messages" in reason
    assert "method=POST" in reason
    assert "bid=" in reason
    assert "cid=" in reason
    assert "via=direct" in reason
    assert "model=claude-opus-4-8" in reason
    assert "type='invalid_request_error'" in reason
    assert "max_tokens must be greater than zero" in reason
    assert "Authorization=<redacted>" in reason
    assert "x-api-key=<redacted>" in reason

    done = next(line for line in lines if line.startswith("done   "))
    assert "method=POST" in done
    assert "path=/v1/messages" in done
    assert "status=400" in done
    assert "bid=" in done
    assert "cid=" in done
    assert "via=direct" in done
    assert "elapsed_ms=" in done
    assert "secret-value" not in "\n".join(lines)
    assert "gsk_reflected_test_secret" not in "\n".join(lines)


async def test_oauth_profile_429_also_skips_message_limiter(
    client: TestClient, monkeypatch
) -> None:
    # The sibling telemetry path /api/oauth/profile gets the same exemption.
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 1)
    monkeypatch.setattr(config, "MAX_CONCURRENT", 3)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 1.0)
    bid = proxy._bearer_id({"authorization": "Bearer profile-poll"})

    resp = await client.get(
        "/api/oauth/profile",
        headers={"Authorization": "Bearer profile-poll", "X-Stub-Mode": "oauth-usage-429"},
    )
    await resp.read()
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)

    assert resp.status == 429
    lim = config.bearer_limiters[bid]
    assert lim.max_concurrent == 3
    assert lim.retry_after_remaining() == 0.0


async def test_retry_after_before_queue_reroutes_to_fresh_account(
    client: TestClient, monkeypatch
) -> None:
    _force_single_slot_fair_queue(monkeypatch)
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/fake/a.json,B:/fake/b.json")

    raw_a = "sk-ant-oat01-PRE-SLOT-A"
    raw_b = "sk-ant-oat01-PRE-SLOT-B"
    bid_a = proxy._bearer_id({"Authorization": f"Bearer {raw_a}"})
    bid_b = proxy._bearer_id({"Authorization": f"Bearer {raw_b}"})
    account_a = {"label": "A", "token": raw_a, "bearer_id": bid_a}
    account_b = {"label": "B", "token": raw_b, "bearer_id": bid_b}
    route_calls = 0

    def routing_snapshot(_now=None):
        nonlocal route_calls
        route_calls += 1
        return [account_a] if route_calls == 1 else [account_a, account_b]

    monkeypatch.setattr(accounts, "routing_snapshot", routing_snapshot)
    lim_a = await proxy._get_bearer_limiter(bid_a, "fair", config.MAX_CONCURRENT)
    lim_a.note_retry_after(3600)

    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={"Authorization": f"Bearer {raw_a}"},
    )

    assert status == 200
    assert b"upstream retry-after window is active" not in streamed
    assert route_calls >= 2
    assert config.bearer_state[bid_a]["served"] == 0
    assert config.bearer_state[bid_b]["served"] == 1


async def test_active_long_retry_after_window_fails_fast_after_queue(
    client: TestClient, monkeypatch
) -> None:
    _force_single_slot_fair_queue(monkeypatch)
    bid = proxy._bearer_id({"authorization": "Bearer postslot-long-retry"})
    lim = await proxy._get_bearer_limiter(bid, "fair", config.MAX_CONCURRENT)

    async with lim.slot("holder"):
        task = asyncio.create_task(
            _post_and_settle(
                client,
                data=b'{"model":"claude-sonnet-4-6"}',
                headers={"Authorization": "Bearer postslot-long-retry"},
            )
        )
        await _wait_for_limiter_queued(lim, 1)
        assert lim.snapshot()["queued_total"] == 1
        lim.note_retry_after(3600)

    status, streamed = await asyncio.wait_for(task, timeout=1.0)

    assert status == 429
    assert b"holding the local gateway request" in streamed
    assert config.state["queued"] == 0
    assert config.state["inflight"] == 0
    snap = lim.snapshot()
    assert snap["queued_total"] == 0
    assert snap["inflight"] == 0


async def test_retry_after_armed_post_slot_reenters_without_holding_slot(
    client: TestClient, monkeypatch
) -> None:
    """A concurrent hard Retry-After must not sleep inside an inflight slot.

    Reproduce the narrow race after the queued request has acquired its slot
    and completed the first two admission checks. The third path check arms a
    long window, exactly where the old unconditional ``wait_retry_after`` then
    slept until reset. The request must instead return inside the queue budget,
    without upstream dispatch or leaked accounting.
    """
    _force_single_slot_fair_queue(monkeypatch)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 0.2)
    raw_bearer = "post-slot-race"
    authorization = f"Bearer {raw_bearer}"
    bid = proxy._bearer_id({"authorization": authorization})
    lim = await proxy._get_bearer_limiter(bid, "fair", config.MAX_CONCURRENT)
    real_blocks_path = proxy._retry_after_blocks_path
    in_slot_checks = 0

    def arm_on_final_admission_check(path: str) -> bool:
        nonlocal in_slot_checks
        if config.state["inflight"]:
            in_slot_checks += 1
            if in_slot_checks == 3:
                lim.note_retry_after(1004)
        return real_blocks_path(path)

    monkeypatch.setattr(proxy, "_retry_after_blocks_path", arm_on_final_admission_check)
    started = time.monotonic()
    status, streamed = await asyncio.wait_for(
        _post_and_settle(
            client,
            data=b'{"model":"claude-sonnet-4-6"}',
            headers={"Authorization": authorization},
        ),
        timeout=1.0,
    )

    assert time.monotonic() - started < 1.0
    assert status == 429
    assert b"holding the local gateway request" in streamed
    assert client.probe_state["attempts"].get(authorization, 0) == 0
    assert config.state["queued"] == 0
    assert config.state["inflight"] == 0
    snap = lim.snapshot()
    assert snap["queued_total"] == 0
    assert snap["inflight"] == 0


async def test_retry_after_after_queue_reroutes_to_fresh_account(
    client: TestClient, monkeypatch
) -> None:
    _force_single_slot_fair_queue(monkeypatch)
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/fake/a.json,B:/fake/b.json")

    raw_a = "sk-ant-oat01-POST-SLOT-A"
    raw_b = "sk-ant-oat01-POST-SLOT-B"
    bid_a = proxy._bearer_id({"Authorization": f"Bearer {raw_a}"})
    bid_b = proxy._bearer_id({"Authorization": f"Bearer {raw_b}"})
    account_a = {"label": "A", "token": raw_a, "bearer_id": bid_a}
    account_b = {"label": "B", "token": raw_b, "bearer_id": bid_b}
    route_b = False

    def routing_snapshot(_now=None):
        return [account_a, account_b] if route_b else [account_a]

    monkeypatch.setattr(accounts, "routing_snapshot", routing_snapshot)
    bid = proxy._bearer_id({"authorization": f"Bearer {raw_a}"})
    lim_a = await proxy._get_bearer_limiter(bid, "fair", config.MAX_CONCURRENT)

    async with lim_a.slot("holder"):
        task = asyncio.create_task(
            _post_and_settle(
                client,
                data=b'{"model":"claude-sonnet-4-6"}',
                headers={"Authorization": f"Bearer {raw_a}"},
            )
        )
        await _wait_for_limiter_queued(lim_a, 1)
        assert lim_a.snapshot()["queued_total"] == 1
        route_b = True
        lim_a.note_retry_after(3600)

    status, streamed = await asyncio.wait_for(task, timeout=1.0)

    assert status == 200
    assert b"holding the local gateway request" not in streamed
    assert config.state["queued"] == 0
    assert config.state["inflight"] == 0
    assert config.bearer_state[bid_a]["served"] == 0
    assert config.bearer_state[bid_b]["served"] == 1
    assert lim_a.snapshot()["queued_total"] == 0
    assert lim_a.snapshot()["inflight"] == 0


async def test_short_window_holds_on_b_without_rerouting_to_dead_a(
    client: TestClient, monkeypatch
) -> None:
    """The 15/07/2026 incident, end to end: a request arriving on the DEAD
    incoming account A (long window) must route to B and HOLD through B's short
    window, never hop back onto A's 41 h window and fast-fail. Exercises the
    handler loop: routing -> reroute-to-self -> fast-fail-declines -> hold."""
    _force_single_slot_fair_queue(monkeypatch)  # MAX_HOLD_RETRY_AFTER_S = 1.0
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/fake/a.json,B:/fake/b.json")

    raw_a = "sk-ant-oat01-HOLD-A"
    raw_b = "sk-ant-oat01-HOLD-B"
    bid_a = proxy._bearer_id({"Authorization": f"Bearer {raw_a}"})
    bid_b = proxy._bearer_id({"Authorization": f"Bearer {raw_b}"})
    account_a = {"label": "A", "token": raw_a, "bearer_id": bid_a}
    account_b = {"label": "B", "token": raw_b, "bearer_id": bid_b}
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: [account_a, account_b])

    lim_a = await proxy._get_bearer_limiter(bid_a, "fair", config.MAX_CONCURRENT)
    lim_a.note_retry_after(3600)  # A dead: window >> MAX_HOLD → never holdable
    lim_b = await proxy._get_bearer_limiter(bid_b, "fair", config.MAX_CONCURRENT)
    lim_b.note_retry_after(0.05)  # B: short holdable window

    status, streamed = await _post_and_settle(
        client,
        data=b'{"model":"claude-sonnet-4-6"}',
        headers={"Authorization": f"Bearer {raw_a}"},
    )

    assert status == 200
    assert b"holding the local gateway request" not in streamed
    assert b"upstream retry-after window is active" not in streamed
    assert config.bearer_state[bid_a]["served"] == 0  # never hopped to dead A
    assert config.bearer_state[bid_b]["served"] == 1  # held + served on B


async def test_queue_wait_timeout_fails_fast_with_clean_503(
    client: TestClient, monkeypatch
) -> None:
    """A request parked past QUEUE_MAX_WAIT_S gets a clean 503 + Retry-After
    while its transport is still alive. Pins the 07/07/2026 phantom-401 path:
    the unbounded queue wait outlived claude's ~60 s socket patience, the late
    write hit a closing transport, and the truncated HTTP surfaced client-side
    as InvalidHTTPResponse → misread as a 401 login failure.
    """
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 1)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 0.2)
    bid = proxy._bearer_id({"authorization": "Bearer queue-wait-timeout"})
    lim = await proxy._get_bearer_limiter(bid, "fair", config.MAX_CONCURRENT)

    async with lim.slot("holder"):
        resp = await client.post(
            "/v1/messages",
            data=b'{"model":"claude-opus-4-8"}',
            headers={"Authorization": "Bearer queue-wait-timeout"},
        )
        status = resp.status
        streamed = await resp.read()
        resp_headers = resp.headers

    assert status == 503
    assert resp_headers["retry-after"] == str(config.QUEUE_TIMEOUT_RETRY_AFTER_S)
    assert resp_headers[config.QUEUE_TIMEOUT_HEADER] == "1"
    assert b"queue wait exceeded" in streamed
    assert (config.state["queued"], config.state["inflight"]) == (0, 0)
    snap = lim.snapshot()
    # queue rolled back, no slot consumed, and — because an admission timeout
    # is not upstream pushback — the live cap untouched, no throttle recorded.
    assert (snap["queued_total"], snap["inflight"], snap["max_concurrent"]) == (0, 0, 1)
    assert snap["last_throttle_at"] == 0.0


async def test_queue_wait_budget_forwarded_minus_local_spend(
    client: TestClient, monkeypatch
) -> None:
    """The next tier receives only the REMAINING wait budget, so local+central
    bounds cannot stack past the client's patience (Codex BLOCKER, PR #83)."""
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)
    # Only CENTRAL sends carry the budget header; point central at the stub,
    # which echoes every budget value it received.
    monkeypatch.setattr(config, "CENTRAL_URL", config.UPSTREAM)
    resp = await client.post(
        "/v1/messages",
        data=b'{"model":"claude-opus-4-8"}',
        headers={"Authorization": "Bearer budget-forward"},
    )
    echoed = resp.headers.get("x-echo-wait-budget")
    await resp.read()
    assert resp.status == 200
    assert echoed is not None
    assert 0 < int(echoed) <= 30_000


async def test_mixed_case_client_budget_cannot_outlive_the_stamp(
    client: TestClient, monkeypatch
) -> None:
    """A client's mixed-case budget copy must not coexist with the canonical
    stamp — the next tier's CIMultiDict.get() would read the client's value
    first and defeat the min() (Codex round-2 BLOCKER, PR #83)."""
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)
    monkeypatch.setattr(config, "CENTRAL_URL", config.UPSTREAM)
    resp = await client.post(
        "/v1/messages",
        data=b'{"model":"claude-opus-4-8"}',
        headers={
            "Authorization": "Bearer budget-case",
            "X-Anthropic-Throttle-Wait-Budget-Ms": "90000",
        },
    )
    echoed = resp.headers.get("x-echo-wait-budget")
    await resp.read()
    assert resp.status == 200
    assert echoed is not None
    assert "," not in echoed  # exactly one budget header reached the next tier
    assert 0 < int(echoed) <= 30_000


async def test_inherited_zero_budget_fails_fast_pre_queue(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 30.0)
    resp = await client.post(
        "/v1/messages",
        data=b'{"model":"claude-opus-4-8"}',
        headers={
            "Authorization": "Bearer budget-exhausted",
            config.WAIT_BUDGET_HEADER: "0",
        },
    )
    body = await resp.read()
    assert resp.status == 503
    assert resp.headers[config.QUEUE_TIMEOUT_HEADER] == "1"
    assert b"queue wait exceeded" in body
    assert config.state["queued"] == 0


async def test_spoofed_queue_timeout_header_is_stripped_and_still_shrinks(
    client: TestClient, monkeypatch
) -> None:
    """An upstream 503 asserting the proxy-private header without the proxy
    marker must not skip AIMD (Codex MAJOR, PR #83)."""
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", 0)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    resp = await client.post(
        "/v1/messages",
        data=b'{"model":"claude-opus-4-8"}',
        headers={"Authorization": "Bearer spoofer", "X-Stub-Mode": "spoof-queue-timeout"},
    )
    await resp.read()
    for _ in range(20):
        await asyncio.sleep(0)
    assert resp.status == 503
    assert config.QUEUE_TIMEOUT_HEADER not in resp.headers
    bid = proxy._bearer_id({"authorization": "Bearer spoofer"})
    lim = config.bearer_limiters[bid]
    assert lim.max_concurrent < 3


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


async def _post_unified_high(
    client: TestClient, bearer: str
) -> tuple[str, limiter.FairBearerLimiter]:
    status, _ = await _post_and_settle(
        client,
        data=b'{"model":"claude-opus-4-7"}',
        headers={"Authorization": f"Bearer {bearer}", "X-Stub-Mode": "unified-high"},
    )
    assert status == 200
    bid = next(iter(config.bearer_state))
    return bid, config.bearer_limiters[bid]


async def test_unified_proactive_shrink_fires_via_http(client: TestClient, monkeypatch) -> None:
    """FR-008 (Codex PARTIAL #3, PR #30): proactive util-shrink was only covered
    at the _apply_unified unit level. Drive it through the real HTTP path: a
    bearer whose binding 5h window (0.92) crosses UTILIZATION_TARGET (0.85) must
    have its live cap shrunk one CUBIC step below the hard ceiling."""
    # observe/fair so the AIMD ceiling can move (shrink is a no-op in off mode).
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.85)
    bid, lim = await _post_unified_high(client, "oauth-util-high")
    assert config.bearer_state[bid]["unified"]["util_5h"] == 0.92
    assert lim.max_concurrent < lim.hard_max
    assert lim.retry_after_remaining() == 0


async def test_unified_no_shrink_when_target_off_via_http(client: TestClient, monkeypatch) -> None:
    """Mirror of the above with UTILIZATION_TARGET=0 (default): high utilization
    is surfaced but the live cap is untouched — observe-only, no proactive shrink.
    Same observe mode as the positive case, so the only variable is the target."""
    monkeypatch.setattr(config, "QUEUE_MODE", "observe")
    assert proxy.UTILIZATION_TARGET == 0
    bid, lim = await _post_unified_high(client, "oauth-util-off")
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


def _disconnect_request_count(model: str) -> float:
    return (
        proxy.REGISTRY.get_sample_value(
            "anthropic_requests_total",
            {"method": "POST", "status": "499", "model": model},
        )
        or 0.0
    )


async def test_handler_drops_client_disconnects_before_upstream(
    client: TestClient, monkeypatch
) -> None:
    payload = b'{"model":"claude-opus-4-8"}'

    async def raise_reset(_request: web.Request) -> bytes:
        raise ConnectionResetError("reset during upload")

    def upload_reset(patch: pytest.MonkeyPatch) -> None:
        patch.setattr(web.Request, "read", raise_reset)

    def closed_before_forward(patch: pytest.MonkeyPatch) -> None:
        checks = iter([False, True])

        def fake_disconnected(_request: web.Request) -> bool:
            return next(checks, True)

        async def fail_forward(*_args, **_kwargs):  # pragma: no cover - should not run
            raise AssertionError("disconnected request reached upstream forwarding")

        patch.setattr(proxy, "_request_disconnected", fake_disconnected)
        patch.setattr(proxy, "_forward_with_retry", fail_forward)

    for bearer, configure in (
        ("upload-reset", upload_reset),
        ("closed-before-forward", closed_before_forward),
    ):
        metric_model = "unknown" if bearer == "upload-reset" else "claude-opus-4-8"
        previous_total = _disconnect_request_count(metric_model)
        keys = "client_disconnects upstream_retries served inflight queued".split()
        _reset_proxy_state()
        with monkeypatch.context() as patch:
            configure(patch)
            auth = {"Authorization": f"Bearer {bearer}"}
            response = await client.post("/v1/messages", data=payload, headers=auth)
            assert response.status == 499
            if bearer == "closed-before-forward":
                bid = proxy._bearer_id(auth)
                lim = config.bearer_limiters[bid]
                assert lim.retry_probe_required() is True
                assert lim.retry_probe_inflight() is False
                assert lim.try_begin_retry_probe() is True
        assert [config.state[key] for key in keys] == [1, 0, 0, 0, 0]
        assert _disconnect_request_count(metric_model) == previous_total + 1
