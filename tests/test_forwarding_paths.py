"""Coverage for the harder forwarding branches: central failover, the central
health loop, burst pacing, the 502 exhausted-retry path, and the advisor UI
route when enabled. These exercise code the happy-path suite doesn't reach,
without changing any runtime behaviour.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, forwarding, limiter, pacing, proxy
from anthropic_throttle_proxy.ui import advisor_impl
from anthropic_throttle_proxy.ui.routes import attach_ui

_SSE_BODY = (
    b'event: message_start\ndata: {"type":"message_start","message":'
    b'{"usage":{"input_tokens":4}}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _reset() -> None:
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


def _ok_upstream() -> web.Application:
    async def messages(request: web.Request) -> web.StreamResponse:
        await request.read()
        resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_SSE_BODY)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", messages)
    return app


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


async def _post_messages(
    tc: TestClient, bearer: str, model: str = "claude-haiku-4-5"
) -> tuple[aiohttp.ClientResponse, bytes]:
    """POST a minimal /v1/messages body, drain it, and settle background tasks."""
    resp = await tc.post(
        "/v1/messages",
        data=f'{{"model":"{model}"}}'.encode(),
        headers={"Authorization": f"Bearer {bearer}"},
    )
    body = await resp.read()
    await _settle()
    return resp, body


@pytest.fixture
async def env(monkeypatch):
    """Live stub upstream + a bound, route-wired proxy app + TestClient."""
    upstream = TestServer(_ok_upstream())
    await upstream.start_server()
    upstream_url = str(upstream.make_url("")).rstrip("/")

    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "UPSTREAM", upstream_url)
    _reset()

    app = web.Application()
    app.on_response_prepare.append(forwarding.stamp_proxy_marker)  # as proxy.main() does
    app.router.add_get("/__throttle/health", proxy.health)
    app.router.add_get("/metrics", proxy.metrics)
    attach_ui(app)
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    server = TestServer(app)
    tc = TestClient(server)
    await tc.start_server()
    try:
        yield tc, upstream_url
    finally:
        await tc.close()
        await upstream.close()


async def test_central_failover_marks_down_and_retries_direct(env, monkeypatch) -> None:
    tc, upstream_url = env
    # Pretend a central tier is configured + healthy, but point it at a dead
    # port so the forward fails → handler marks it DOWN and retries direct.
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")  # unroutable
    config.state["central_status"] = "up"
    resp, body = await _post_messages(tc, "central-fail")
    assert resp.status == 200  # direct retry succeeded against the stub
    assert b"message_stop" in body
    assert config.state["central_status"] == "down"
    assert config.state["upstream_retries"] >= 1


@pytest.mark.parametrize(
    ("relay_headers", "expected_central_status"),
    [
        # Bare 502 = dokku nginx answering for a dead container → mark DOWN.
        ({}, "down"),
        # Marked 502 = live central relaying Anthropic's own edge error →
        # retry direct for this request but keep central routable. Force-
        # marking DOWN here stampeded the fleet past the central semaphore
        # (05/07/2026 incident).
        ({config.MARKER_HEADER: "1"}, "up"),
    ],
)
async def test_central_502_markdown_depends_on_marker(
    env, monkeypatch, relay_headers, expected_central_status
) -> None:
    tc, upstream_url = env
    sensitive_marker = "central-reflected-secret"
    lines: list[str] = []
    monkeypatch.setattr(proxy, "log", lines.append)

    async def central_error(_request: web.Request) -> web.Response:
        return web.Response(
            status=502,
            text=f"upstream error: Authorization: Bearer {sensitive_marker}",
            headers=relay_headers,
        )

    central = TestServer(web.Application())
    central.app.router.add_route("*", "/{path:.*}", central_error)
    await central.start_server()
    try:
        monkeypatch.setattr(config, "CENTRAL_URL", str(central.make_url("")).rstrip("/"))
        monkeypatch.setattr(config, "UPSTREAM", upstream_url)
        config.state["central_status"] = "up"

        resp, body = await _post_messages(tc, "central-502")

        assert resp.status == 200  # direct retry succeeded against the stub
        assert b"message_stop" in body
        assert config.state["central_status"] == expected_central_status
        assert config.state["upstream_retries"] >= 1
        assert sensitive_marker not in "\n".join(lines)
    finally:
        await central.close()


async def test_marker_stamped_on_every_response(env) -> None:
    """main() registers stamp_proxy_marker app-wide; generated responses
    (health) and streamed relays must both carry the marker header."""
    tc, _ = env
    resp = await tc.get("/__throttle/health")
    assert resp.status == 200
    assert resp.headers.get(config.MARKER_HEADER) == "1"


async def test_central_health_marks_bad_upstream_egress_unhealthy(monkeypatch) -> None:
    _reset()
    monkeypatch.setattr(config, "CENTRAL_URL", "http://central.example")
    responses: list[web.Response] = []

    class FakeHealthResponse:
        status = 503

        async def json(self, *, content_type=None):  # noqa: ARG002 - matches aiohttp API
            return {
                "upstream_egress_ok": False,
                "upstream_egress_error": "gaierror(-3)",
            }

    class FakeSession:
        def get(self, _url: str):
            class ResponseContext:
                async def __aenter__(self):
                    response = FakeHealthResponse()
                    responses.append(response)
                    return response

                async def __aexit__(self, *_exc):
                    return False

            return ResponseContext()

    await forwarding._poll_central_once(FakeSession())  # type: ignore[arg-type]

    assert config.state["central_status"] == "unknown"
    await forwarding._poll_central_once(FakeSession())  # type: ignore[arg-type]
    await forwarding._poll_central_once(FakeSession())  # type: ignore[arg-type]
    assert config.state["central_status"] == "down"


async def test_central_down_off_mode_uses_local_fair_queue(env, monkeypatch) -> None:
    tc, _ = env
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")
    config.state["central_status"] = "down"

    resp, _ = await _post_messages(tc, "central-down")

    assert resp.status == 200
    bid = next(iter(config.bearer_limiters))
    lim = config.bearer_limiters[bid]
    assert lim.queue_mode == "fair"
    assert lim.queue_enabled is True
    assert lim.observe_enabled is True
    assert lim.hard_max == config.CENTRAL_LOCAL_MAX_CONCURRENT


async def test_central_up_off_mode_still_uses_local_safety_queue(env, monkeypatch) -> None:
    tc, upstream_url = env
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 32)
    monkeypatch.setattr(config, "CENTRAL_LOCAL_MAX_CONCURRENT", 2)
    monkeypatch.setattr(config, "CENTRAL_URL", upstream_url)
    config.state["central_status"] = "up"

    resp, _ = await _post_messages(tc, "central-up", model="claude-opus-4-7[1m]")

    assert resp.status == 200
    bid = next(iter(config.bearer_limiters))
    lim = config.bearer_limiters[bid]
    assert lim.queue_mode == "fair"
    assert lim.queue_enabled is True
    assert lim.hard_max == 2
    assert lim.max_concurrent == config.AIMD_INITIAL_CONCURRENT


async def test_existing_limiter_raises_when_central_local_cap_increases(env, monkeypatch) -> None:
    tc, upstream_url = env
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 32)
    monkeypatch.setattr(config, "CENTRAL_LOCAL_MAX_CONCURRENT", 1)
    monkeypatch.setattr(config, "CENTRAL_URL", upstream_url)
    config.state["central_status"] = "up"

    for _ in range(2):
        resp, _ = await _post_messages(tc, "retune-central-local")
        assert resp.status == 200
        monkeypatch.setattr(config, "CENTRAL_LOCAL_MAX_CONCURRENT", 4)

    bid = next(iter(config.bearer_limiters))
    lim = config.bearer_limiters[bid]
    assert lim.hard_max == 4
    assert lim.max_concurrent == config.AIMD_INITIAL_CONCURRENT


async def test_runtime_override_retunes_existing_central_local_limiter(
    env, monkeypatch, tmp_path
) -> None:
    tc, upstream_url = env
    monkeypatch.setattr(config, "OVERRIDES_FILE", tmp_path / "overrides.json")
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "MAX_CONCURRENT", 32)
    monkeypatch.setattr(config, "CENTRAL_LOCAL_MAX_CONCURRENT", 1)
    monkeypatch.setattr(config, "CENTRAL_URL", upstream_url)
    config.state["central_status"] = "up"

    resp, _ = await _post_messages(tc, "hot-retune-central-local")
    assert resp.status == 200

    bid = next(iter(config.bearer_limiters))
    lim = config.bearer_limiters[bid]
    assert lim.hard_max == 1

    config.set_override("central_local_max_concurrent", "5")
    await _settle()

    assert lim.hard_max == 5
    assert lim.max_concurrent == config.AIMD_INITIAL_CONCURRENT
    config.RUNTIME_OVERRIDES.pop("central_local_max_concurrent", None)


async def test_fallback_promotion_does_not_downgrade_on_central_recovery(env, monkeypatch) -> None:
    tc, _ = env
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")
    config.state["central_status"] = "down"

    await _post_messages(tc, "sticky-fair")

    config.state["central_status"] = "up"
    await _post_messages(tc, "sticky-fair")

    bid = next(iter(config.bearer_limiters))
    assert config.bearer_limiters[bid].queue_mode == "fair"


async def test_central_failure_direct_retry_is_serialized(monkeypatch) -> None:
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    monkeypatch.setattr(config, "CENTRAL_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(config, "UPSTREAM", "http://direct.example")
    monkeypatch.setattr(proxy, "_direct_fallback_lock", None)
    config.state["central_status"] = "up"
    config.state["upstream_retries"] = 0

    active = 0
    max_active = 0

    async def fake_try_forward(
        _request, _headers, _body, _url, _timeout, attempt, retryable_statuses=None
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        attempt.final_status = 200
        response = web.Response(status=200, text="ok")
        attempt.response = response
        return response, None

    monkeypatch.setattr(proxy, "_try_forward", fake_try_forward)
    timeout = aiohttp.ClientTimeout(total=1)

    class FakeRequest:
        query_string = ""

    async def one_retry() -> web.StreamResponse | web.Response:
        return await proxy._retry_direct_once(
            FakeRequest(),
            {},
            None,
            "v1/messages",
            "central",
            "http://127.0.0.1:1/v1/messages",
            timeout,
            RuntimeError("central down"),
            proxy._Attempt(),
        )

    responses = await asyncio.gather(*(one_retry() for _ in range(4)))

    assert [r.status for r in responses] == [200, 200, 200, 200]
    assert max_active == 1
    assert config.state["upstream_retries"] == 4


async def test_exhausted_retry_returns_502(env, monkeypatch) -> None:
    tc, _ = env
    # No central; point the direct upstream at a dead port so BOTH the initial
    # attempt and the single direct retry fail → final 502.
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "UPSTREAM", "http://127.0.0.1:1")
    resp = await tc.post(
        "/v1/messages",
        data=b'{"model":"claude-haiku-4-5"}',
        headers={"Authorization": "Bearer dead-upstream"},
    )
    text = await resp.text()
    await _settle()
    assert resp.status == 502
    assert "upstream error" in text
    assert config.state["upstream_retries"] >= 1


async def test_forward_once_propagates_client_disconnect(monkeypatch) -> None:
    """Regression: local client disconnects must not look like central failure.

    Under a many-pane Claude burst, `StreamResponse.write()` can raise
    ClientConnectionResetError("Cannot write to closing transport") after the
    client side gives up. That is not evidence that central is unhealthy; if
    `_forward_once` turns it into an upstream error, the caller marks central
    down and retries direct, wasting throughput and bypassing fleet admission.
    """

    async def ok(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(text="ok")

    async def raise_client_disconnect(
        _request: web.Request, _upstream: aiohttp.ClientResponse
    ) -> forwarding.ForwardResult:
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(forwarding, "_stream_response", raise_client_disconnect)

    upstream = TestServer(web.Application())
    upstream.app.router.add_route("*", "/{path:.*}", ok)
    await upstream.start_server()

    class FakeRequest:
        method = "POST"

    try:
        with pytest.raises(aiohttp.ClientConnectionResetError):
            await forwarding._forward_once(
                FakeRequest(),
                {},
                b"{}",
                str(upstream.make_url("/v1/messages")),
                aiohttp.ClientTimeout(total=2),
            )
    finally:
        await upstream.close()


async def test_pace_dispatch_enforces_gap(monkeypatch) -> None:
    pacing.set_lock(asyncio.Lock())
    pacing._last_dispatch_ts = 0.0
    monkeypatch.setattr(config, "MIN_DISPATCH_GAP_S", 0.05)
    # First call sets the timestamp (no wait); second must wait ~50 ms.
    await pacing._pace_dispatch()
    t0 = asyncio.get_running_loop().time()
    await pacing._pace_dispatch()
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed >= 0.04


async def test_pace_dispatch_noop_when_disabled(monkeypatch) -> None:
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "MIN_DISPATCH_GAP_S", 0.0)
    t0 = asyncio.get_running_loop().time()
    await pacing._pace_dispatch()
    assert asyncio.get_running_loop().time() - t0 < 0.02


async def test_central_health_loop_noop_without_central(monkeypatch) -> None:
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    # Returns immediately — must not raise or hang.
    await asyncio.wait_for(forwarding.central_health_loop(), timeout=1.0)


def test_pick_target_prefers_central_during_cold_start(monkeypatch) -> None:
    """Cold-start central_status=unknown must not leak the first request direct.

    The health loop adopts central on its first good probe, but live restarts can
    receive queued systemd-socket requests before that probe runs. Those requests
    still need fleet-wide central admission; only an explicit DOWN routes direct.
    """
    _reset()
    monkeypatch.setattr(config, "CENTRAL_URL", "http://central.example")
    monkeypatch.setattr(config, "UPSTREAM", "https://api.example")

    url, _timeout, via = forwarding.pick_target("v1/messages", "beta=1")
    assert via == "central"
    assert url == "http://central.example/v1/messages?beta=1"

    config.state["central_status"] = "down"
    url, _timeout, via = forwarding.pick_target("v1/messages", "")
    assert via == "direct"
    assert url == "https://api.example/v1/messages"


async def test_poll_central_once_up_then_down(monkeypatch) -> None:
    # Pin thresholds so the assertions don't depend on ambient env (the legacy
    # 1/1 setting would otherwise change which probe count flips the status).
    monkeypatch.setattr(config, "CENTRAL_HEALTH_OK_THRESHOLD", 2)
    monkeypatch.setattr(config, "CENTRAL_HEALTH_FAIL_THRESHOLD", 3)
    _reset()
    health_app = web.Application()

    async def ok_health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    health_app.router.add_get("/__throttle/health", ok_health)
    server = TestServer(health_app)
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    try:
        timeout = aiohttp.ClientTimeout(total=2)
        config.CENTRAL_URL = url
        config.state["central_status"] = "unknown"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Cold start adopts central on the FIRST healthy probe (the OK_THRESHOLD
            # delay applies only on recovery from down — see the recovery test).
            await forwarding._poll_central_once(session)
        assert config.state["central_status"] == "up"
    finally:
        await server.close()
        config.CENTRAL_URL = ""

    # Now an unreachable central → DOWN only after FAIL_THRESHOLD consecutive misses.
    config.CENTRAL_URL = "http://127.0.0.1:1"
    timeout = aiohttp.ClientTimeout(total=0.3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(config.CENTRAL_HEALTH_FAIL_THRESHOLD - 1):
            await forwarding._poll_central_once(session)
            assert config.state["central_status"] == "up"  # still up under threshold
        await forwarding._poll_central_once(session)
    assert config.state["central_status"] == "down"
    config.CENTRAL_URL = ""


async def test_central_cold_start_adopts_on_first_probe(monkeypatch) -> None:
    """Regression (Codex review, PR #35): from the initial 'unknown' state the
    first healthy probe must flip to UP even with OK_THRESHOLD>1. Otherwise every
    restart/deploy routes traffic direct-to-upstream for a full health interval —
    the exact unqueued firehose the hysteresis is meant to prevent."""
    monkeypatch.setattr(config, "CENTRAL_HEALTH_OK_THRESHOLD", 5)
    _reset()  # central_status == "unknown"
    forwarding._record_central_sample(True)
    assert config.state["central_status"] == "up"
    assert config.state["central_consecutive_ok"] == 1


async def test_central_recovery_from_down_needs_ok_threshold(monkeypatch) -> None:
    """Re-adoption after a DOWN is guarded by OK_THRESHOLD (don't trust a flapping
    central immediately) — unlike cold start, which adopts on the first probe."""
    monkeypatch.setattr(config, "CENTRAL_HEALTH_OK_THRESHOLD", 2)
    _reset()
    config.state["central_status"] = "down"

    forwarding._record_central_sample(True)
    assert config.state["central_status"] == "down"  # 1 ok < OK_THRESHOLD(2)
    forwarding._record_central_sample(True)
    assert config.state["central_status"] == "up"  # 2nd consecutive ok clears it


async def test_central_single_probe_blip_does_not_flap(monkeypatch) -> None:
    """Regression (Codex PARTIAL #1, PR #30): a lone failed probe while central
    is healthy must NOT flip status to DOWN — that flapped the whole local fleet
    to direct fallback on 27/05/2026 even though central was serving traffic."""
    monkeypatch.setattr(config, "CENTRAL_HEALTH_FAIL_THRESHOLD", 3)
    _reset()
    config.state["central_status"] = "up"

    # One transient miss, well under FAIL_THRESHOLD → still UP, fallback unchanged.
    forwarding._record_central_sample(False, "transient")
    assert config.state["central_status"] == "up"
    assert config.state["central_consecutive_fail"] == 1

    # A single recovery sample clears the fail streak.
    forwarding._record_central_sample(True)
    assert config.state["central_status"] == "up"
    assert config.state["central_consecutive_fail"] == 0


async def test_ui_advisor_enabled_returns_recommendation(env, monkeypatch) -> None:
    tc, _ = env
    monkeypatch.setenv("ADVISOR_ENABLED", "true")

    async def fake_recommend(_snapshot):
        return "raise THROTTLE_MIN_DISPATCH_GAP_MS to 50"

    monkeypatch.setattr(advisor_impl, "recommend", fake_recommend)
    resp = await tc.post("/ui/advisor")
    assert resp.status == 200
    assert "THROTTLE_MIN_DISPATCH_GAP_MS" in await resp.text()


async def test_ui_advisor_enabled_surfaces_error(env, monkeypatch) -> None:
    """Recommendation failure renders an HTML error partial at 200 so HTMX
    swaps it into #advisor-out. See test_ui_advisor_disabled_renders_inline_error."""
    tc, _ = env
    monkeypatch.setenv("ADVISOR_ENABLED", "true")

    async def boom(_snapshot):
        raise RuntimeError("groq exploded")

    monkeypatch.setattr(advisor_impl, "recommend", boom)
    resp = await tc.post("/ui/advisor")
    assert resp.status == 200
    body = await resp.text()
    assert "advisor-output err" in body
    assert "groq exploded" in body
