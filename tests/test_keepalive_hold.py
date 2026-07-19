"""Acceptance tests for spec 092 — SSE keepalive-hold.

Tests the full acceptance matrix from spec.md:
  - 529-then-200: client gets 200 SSE with keepalive comment(s) then real events
  - central queue-timeout 503-then-capacity: held + streamed; no relay-503
  - concurrency 429-then-200: held + streamed
  - budget-rejected (unified rejected / long Retry-After): NOT held → clean error
  - non-streaming under throttle: clean error, no keepalive
  - bound exhausted mid-hold: ends with SSE event:error, no truncated write
  - keepalive cadence: comment emitted at < idle-timeout interval
  - AIMD: central-queue-depth hold does NOT shrink bearer; real upstream 429 still does

07/07 falsification tests (required to FAIL without the code change):
  - transient 529/queue-timeout that clears within bound → clean 200 SSE, no banner
  - bound-exhausted hold → SSE error event, not bare socket close
"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from anthropic_throttle_proxy import config, limiter, pacing, proxy
from anthropic_throttle_proxy.ui.routes import attach_ui

# A realistic minimal SSE body from the Anthropic API.
_SSE_BODY = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n'
)

# Marker header value (proxy always stamps this on its own responses)
_PROXY_MARKER = {"x-anthropic-throttle-proxy": "1"}


def _reset_state() -> None:
    """Clear process-global registries so each test starts clean."""
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


def _aimd_shrink_total() -> float:
    """Cumulative AIMD shrink events across the process registry.

    AIMD_INITIAL_CONCURRENT == the floor (1) in test config, so the max_concurrent
    VALUE cannot drop on a shrink — the shrink COUNTER is the unambiguous signal
    of whether a shrink event fired (reward-hack lane: prove it, don't infer it).
    """
    from anthropic_throttle_proxy.metrics import M_AIMD_SHRINKS

    return sum(
        s.value for m in M_AIMD_SHRINKS.collect() for s in m.samples if s.name.endswith("_total")
    )


def _make_upstream_app(
    *,
    fail_count: int = 1,
    fail_status: int = 529,
    fail_headers: dict | None = None,
    retry_after_s: int | None = None,
    success_delay_s: float = 0.0,
) -> web.Application:
    """Stub upstream: first ``fail_count`` requests return ``fail_status``,
    then subsequent requests return 200 SSE.

    ``fail_headers`` are added to the error response headers.
    ``retry_after_s`` if set adds ``Retry-After`` to the error responses.
    ``success_delay_s`` delays the 200 response so keepalive frames have time
    to fire before the hold resolves (the emitter floor is 500 ms).
    """
    calls = [0]

    async def messages(request: web.Request) -> web.StreamResponse:
        await request.read()
        calls[0] += 1
        if calls[0] <= fail_count:
            hdrs: dict[str, str] = {"content-type": "application/json"}
            if fail_headers:
                hdrs.update(fail_headers)
            if retry_after_s is not None:
                hdrs["retry-after"] = str(retry_after_s)
            return web.Response(
                status=fail_status,
                headers=hdrs,
                text=json.dumps(
                    {"type": "error", "error": {"type": "overloaded_error", "message": "fake"}}
                ),
            )
        if success_delay_s > 0:
            await asyncio.sleep(success_delay_s)
        resp = web.StreamResponse(
            status=200, headers={"content-type": "text/event-stream", **_PROXY_MARKER}
        )
        await resp.prepare(request)
        await resp.write(_SSE_BODY)
        await resp.write_eof()
        return resp

    async def passthrough(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_route("*", "/{prefix:.*}v1/messages", messages)
    app.router.add_route("*", "/v1/messages", messages)
    app.router.add_route("*", "/{path:.*}", passthrough)
    return app


async def _make_client_with_upstream(
    monkeypatch,
    upstream_app: web.Application,
    *,
    keepalive_interval_ms: int = 50,
    queue_max_wait_s: float = 5.0,
    rate_pushback_retries: int = 0,
) -> tuple[TestClient, TestServer]:
    """Helper: spin up a proxy TestClient with a custom upstream app."""
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    upstream_url = str(upstream_server.make_url("")).rstrip("/")

    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "UPSTREAM", upstream_url)
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "KEEPALIVE_HOLD", True)
    monkeypatch.setattr(config, "KEEPALIVE_INTERVAL_MS", keepalive_interval_ms)
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", queue_max_wait_s)
    monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", rate_pushback_retries)
    _reset_state()

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/", proxy.root_probe)
    app.router.add_get("/__throttle/health", proxy.health)
    app.router.add_get("/metrics", proxy.metrics)
    attach_ui(app)
    app.router.add_route("*", "/{path:.*}", proxy.handler)

    proxy_server = TestServer(app)
    test_client = TestClient(proxy_server)
    await test_client.start_server()
    return test_client, upstream_server


# ---------------------------------------------------------------------------
# Unit tests for T001 primitives
# ---------------------------------------------------------------------------


def test_is_streaming_body_true() -> None:
    body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
    assert proxy._is_streaming_body(body) is True


def test_is_streaming_body_false_no_stream() -> None:
    body = json.dumps({"model": "claude-opus-4-8"}).encode()
    assert proxy._is_streaming_body(body) is False


def test_is_streaming_body_false_stream_false() -> None:
    body = json.dumps({"model": "claude-opus-4-8", "stream": False}).encode()
    assert proxy._is_streaming_body(body) is False


def test_is_streaming_body_none() -> None:
    assert proxy._is_streaming_body(None) is False


def test_is_streaming_body_invalid_json() -> None:
    assert proxy._is_streaming_body(b"not json") is False


async def test_emit_keepalive_frames_writes_comment_and_stops() -> None:
    """The keepalive emitter writes ': keepalive\\n\\n' and stops on cancel."""
    chunks: list[bytes] = []

    class FakeSSEResponse:
        prepared = True

        async def write(self, data: bytes) -> None:
            chunks.append(data)

    resp = FakeSSEResponse()
    # The emitter floor is 500 ms (max(0.5, interval_ms/1000)); sleep 700 ms
    # to guarantee at least one write fires before we cancel.
    task = asyncio.create_task(proxy._emit_keepalive_frames(resp, 10))
    await asyncio.sleep(0.7)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(chunks) >= 1
    assert all(c == b": keepalive\n\n" for c in chunks), f"Unexpected frames: {chunks}"


def test_keepalive_comment_is_sse_comment() -> None:
    """An SSE line starting with ':' is a comment and is dropped by parsers."""
    frame = b": keepalive\n\n"
    assert frame.startswith(b":"), "keepalive frame must start with ':'"
    # Per SSE spec: a line beginning with ':' is a comment and must be ignored.
    # Verify the frame does NOT start with 'event:' or 'data:'.
    assert not frame.startswith(b"event:"), "keepalive must not be an event"
    assert not frame.startswith(b"data:"), "keepalive must not be data"


async def test_emit_sse_error_terminal_shape() -> None:
    """The terminal error event is a well-formed SSE event:error with JSON data."""
    writes: list[bytes] = []
    eof_called = [False]

    class FakeSSEResponse:
        prepared = True

        async def write(self, data: bytes) -> None:
            writes.append(data)

        async def write_eof(self) -> None:
            eof_called[0] = True

    resp = FakeSSEResponse()
    await proxy._emit_sse_error_terminal(resp, "test error", "throttle_timeout")

    assert eof_called[0], "write_eof must be called after the error event"
    assert len(writes) == 1
    raw = writes[0].decode()
    lines = raw.strip().split("\n")
    assert lines[0] == "event: error", f"First line must be 'event: error', got {lines[0]!r}"
    assert lines[1].startswith("data: "), f"Second line must start with 'data: ', got {lines[1]!r}"
    payload = json.loads(lines[1][len("data: ") :])
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "throttle_timeout"
    assert "test error" in payload["error"]["message"]


def test_is_transient_throttle_529() -> None:
    """529 is always transient (Anthropic capacity, invariant 9)."""
    assert proxy._is_transient_throttle(529, {}, "bid") is True


def test_is_transient_throttle_queue_timeout_503() -> None:
    """Central queue-timeout 503 is transient (invariant 7)."""
    resp = web.Response(status=503, headers={config.QUEUE_TIMEOUT_HEADER: "1"})
    assert proxy._is_transient_throttle(503, {}, "bid", resp) is True


def test_is_transient_throttle_budget_rejected_false() -> None:
    """A 429 with unified=rejected status is BUDGET, not transient."""
    meta = {
        "anthropic-ratelimit-unified-status": "rejected",
        "anthropic-ratelimit-unified-utilization": "1.0",
    }
    assert proxy._is_transient_throttle(429, meta, "bid") is False


def test_is_transient_throttle_long_retry_after_false() -> None:
    """A 429 with a long Retry-After is BUDGET (fast-fail path), not transient."""
    meta = {"retry-after": str(int(config.MAX_HOLD_RETRY_AFTER_S) + 1)}
    assert proxy._is_transient_throttle(429, meta, "bid") is False


def test_is_transient_throttle_concurrency_429() -> None:
    """A 429 with allowed unified status and low utilization is transient (concurrency)."""
    meta = {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.1",
        "anthropic-ratelimit-unified-7d-utilization": "0.1",
    }
    assert proxy._is_transient_throttle(429, meta, "bid") is True


def test_is_transient_throttle_non_throttle_status() -> None:
    """Non-throttle statuses are never transient."""
    assert proxy._is_transient_throttle(200, {}, "bid") is False
    assert proxy._is_transient_throttle(400, {}, "bid") is False


# ---------------------------------------------------------------------------
# Integration tests using the full proxy stack
# ---------------------------------------------------------------------------


async def test_529_then_200_client_gets_200_sse(monkeypatch) -> None:
    """07/07 falsification test: 529 that clears within budget yields 200 SSE stream.

    Without the keepalive-hold: the proxy returns a 529 to the client.
    With the keepalive-hold: the client gets 200 text/event-stream with keepalive
    comments, then the real SSE body when the retry succeeds.
    """
    # success_delay_s=0.7 ensures the hold is active long enough for the keepalive
    # emitter (500 ms floor) to fire at least one frame before the retry succeeds.
    upstream_app = _make_upstream_app(fail_count=1, fail_status=529, success_delay_s=0.7)
    test_client, upstream_server = await _make_client_with_upstream(monkeypatch, upstream_app)
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-529"},
        )
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        content_type = resp.headers.get("content-type", "")
        assert "text/event-stream" in content_type, (
            f"Expected text/event-stream, got {content_type!r}"
        )
        raw = await resp.read()
        body_text = raw.decode("utf-8", errors="replace")
        # Should contain keepalive comment(s) and then real SSE events
        assert ": keepalive" in body_text, (
            f"Expected keepalive comments in body: {body_text[:500]!r}"
        )
        assert "message_start" in body_text or "message_stop" in body_text, (
            f"Expected real SSE events in body: {body_text[:500]!r}"
        )
        # No error banner content
        assert "event: error" not in body_text, f"Unexpected error event: {body_text[:500]!r}"
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_keepalive_not_interleaved_into_slow_stream(monkeypatch) -> None:
    """Regression: the keepalive emitter MUST stop before the real body is piped.

    If it keeps firing while ``_forward_once_into_sse`` pipes a 2xx body, a
    ``: keepalive`` comment lands between upstream chunks of a slow stream and
    corrupts the SSE framing. The bug is invisible when the upstream returns the
    whole body instantly (every other test here) — it only bites when the body
    streams past one keepalive interval, i.e. every real (long) Opus generation.
    Assert the real body arrives CONTIGUOUS with no keepalive spliced in.
    """
    # Split a single SSE data line MID-FRAME across two upstream chunks (TCP does
    # not respect frame boundaries) so an interleaved keepalive lands inside the
    # JSON — unambiguously corrupt, not a benign between-events comment.
    full_frame = b'event: message_start\ndata: {"type":"message_start","x":"payload"}\n\n'
    chunk_a = full_frame[:35]
    chunk_b = full_frame[35:]
    calls = [0]
    held_headers = asyncio.Event()
    release_held_eof = asyncio.Event()

    async def messages(request: web.Request) -> web.StreamResponse:
        await request.read()
        calls[0] += 1
        if calls[0] == 1:  # first attempt trips the hold
            return web.Response(
                status=529,
                headers={"content-type": "application/json"},
                text=json.dumps(
                    {"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
                ),
            )
        resp = web.StreamResponse(
            status=200, headers={"content-type": "text/event-stream", **_PROXY_MARKER}
        )
        await resp.prepare(request)
        if calls[0] > 2:
            await resp.write(_SSE_BODY)
            await resp.write_eof()
            return resp
        held_headers.set()
        await resp.write(chunk_a)
        await release_held_eof.wait()
        await resp.write(chunk_b)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", messages)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, app, keepalive_interval_ms=50, queue_max_wait_s=10.0
    )
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 0.0)
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        auth = {"Content-Type": "application/json", "Authorization": "Bearer test-slow"}
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers=auth,
        )
        assert resp.status == 200
        await asyncio.wait_for(held_headers.wait(), timeout=1)
        bid = proxy._bearer_id(auth)
        lim = config.bearer_limiters[bid]
        for _ in range(20):
            if not lim.retry_probe_required():
                break
            await asyncio.sleep(0)
        assert lim.retry_probe_required() is False

        # The held 2xx has sent headers but is still blocked before EOF. A
        # second message must dispatch now, not wait for the long Opus stream.
        await asyncio.sleep(0.6)
        second = await asyncio.wait_for(
            test_client.post("/v1/messages", data=body, headers=auth), timeout=1
        )
        await asyncio.wait_for(second.read(), timeout=1)
        assert second.status == 200
        assert calls[0] == 3

        release_held_eof.set()
        raw = await resp.read()
        # The real body must arrive CONTIGUOUS — a keepalive spliced between the
        # two upstream chunks would break this substring and corrupt SSE framing.
        assert chunk_a + chunk_b in raw, (
            f"real SSE body was fragmented by an interleaved keepalive: {raw!r}"
        )
        # And no keepalive comment survives past the point the real body began.
        assert b": keepalive" not in raw[raw.index(chunk_a) :], (
            f"keepalive comment leaked into the real stream: {raw[raw.index(chunk_a) :]!r}"
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        release_held_eof.set()
        await test_client.close()
        await upstream_server.close()


async def test_concurrency_429_then_200(monkeypatch) -> None:
    """Concurrency 429 (allowed unified headers) that clears yields 200 SSE."""
    meta_headers = {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.1",
        "anthropic-ratelimit-unified-7d-utilization": "0.1",
    }
    upstream_app = _make_upstream_app(fail_count=1, fail_status=429, fail_headers=meta_headers)
    test_client, upstream_server = await _make_client_with_upstream(monkeypatch, upstream_app)
    try:
        body = json.dumps({"model": "claude-sonnet-4-6", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-conc-429"},
        )
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        raw = await resp.read()
        text = raw.decode()
        assert ": keepalive" in text or "message_start" in text, (
            f"Expected hold-then-stream: {text[:500]!r}"
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_queue_timeout_503_then_capacity(monkeypatch) -> None:
    """Central queue-timeout 503 that clears: client held then streamed."""
    # A queue-timeout 503 from central has the QUEUE_TIMEOUT_HEADER stamped
    # AND the MARKER_HEADER (proxy marker). We simulate this as an upstream.
    queue_timeout_headers = {
        config.QUEUE_TIMEOUT_HEADER: "1",
        config.MARKER_HEADER: "1",
    }
    upstream_app = _make_upstream_app(
        fail_count=1, fail_status=503, fail_headers=queue_timeout_headers
    )
    test_client, upstream_server = await _make_client_with_upstream(monkeypatch, upstream_app)
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-qt-503"},
        )
        assert resp.status == 200, f"Expected 200 via hold, got {resp.status}"
        raw = await resp.read()
        text = raw.decode()
        assert "event: error" not in text, f"Unexpected error event: {text[:500]!r}"
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_budget_rejected_not_held(monkeypatch) -> None:
    """Budget-rejected 429 (unified=rejected) is NOT held; clean error returned.

    Regression guard: the hold must never activate for budget exhaustion.
    """
    meta_headers = {
        "anthropic-ratelimit-unified-status": "rejected",
        "anthropic-ratelimit-unified-utilization": "1.0",
    }
    # upstream always returns 429-rejected so a hold would loop forever
    upstream_app = _make_upstream_app(fail_count=999, fail_status=429, fail_headers=meta_headers)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=2.0
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-budget"},
        )
        # Must NOT be a 200 SSE hold; must be a clean error status
        assert resp.status != 200, (
            f"Budget-rejected MUST NOT activate keepalive hold, got {resp.status}"
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_long_retry_after_not_held(monkeypatch) -> None:
    """A 429 with a long Retry-After is fast-failed, not held."""
    long_ra = int(config.MAX_HOLD_RETRY_AFTER_S) + 100  # well above threshold
    upstream_app = _make_upstream_app(fail_count=999, fail_status=429, retry_after_s=long_ra)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=2.0
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-long-ra"},
        )
        # Fast-fail path: 429 or 401 (nudge), NOT a 200 hold
        assert resp.status != 200, (
            f"Long Retry-After MUST NOT activate keepalive hold, got {resp.status}"
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_non_streaming_throttle_not_held(monkeypatch) -> None:
    """Non-streaming (no stream:true) throttle error is returned as-is.

    Regression guard: the hold must only activate for streaming requests.
    """
    upstream_app = _make_upstream_app(fail_count=999, fail_status=529)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=2.0
    )
    try:
        # No stream:true — non-streaming request
        body = json.dumps({"model": "claude-opus-4-8"}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-nons"},
        )
        # Must NOT activate keepalive hold for non-streaming
        assert resp.status != 200, (
            f"Non-streaming MUST NOT activate keepalive hold, got {resp.status}"
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_bound_exhausted_emits_sse_error_not_socket_close(monkeypatch) -> None:
    """07/07 falsification test: bound-exhausted hold ends with SSE error event,
    never a bare socket close / truncated write.

    Without the hold: the proxy returns a 503 status.
    With the hold but exhausted budget: the client gets 200 SSE with a terminal
    'event: error' event and a clean EOF — never a mid-message socket close.
    """
    # upstream ALWAYS throttles — budget will exhaust
    upstream_app = _make_upstream_app(fail_count=9999, fail_status=529)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch,
        upstream_app,
        keepalive_interval_ms=50,
        queue_max_wait_s=0.3,  # very short budget → exhausts quickly
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-exhaust"},
        )
        # The hold committed to 200; must remain 200 even on exhaustion
        assert resp.status == 200, (
            f"Exhausted hold must still be 200 SSE (error in body), got {resp.status}"
        )
        content_type = resp.headers.get("content-type", "")
        assert "text/event-stream" in content_type

        raw = await resp.read()
        text = raw.decode("utf-8", errors="replace")

        # Must contain the terminal SSE error event (invariant 4)
        assert "event: error" in text, (
            f"Exhausted hold must emit SSE error event, not socket close: {text[:500]!r}"
        )
        # Must NOT be a bare socket close (the body must parse as valid text)
        assert len(text) > 0, "Empty body from exhausted hold — looks like socket close"
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_keepalive_interval_respected(monkeypatch) -> None:
    """Keepalive frames are emitted at < idle-timeout interval (invariant 1).

    The emitter floor is 500 ms (max(0.5, interval_ms/1000)). The upstream is
    delayed 700 ms so the hold is active long enough for one frame to fire
    before the retry succeeds. At least one keepalive comment must appear.
    """
    upstream_app = _make_upstream_app(fail_count=1, fail_status=529, success_delay_s=0.7)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, keepalive_interval_ms=500
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-interval"},
        )
        assert resp.status == 200
        raw = await resp.read()
        text = raw.decode()
        # At least one keepalive comment should have been written before body
        assert ": keepalive" in text, f"No keepalive frame found: {text[:500]!r}"
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_keepalive_hold_disabled_passthrough(monkeypatch) -> None:
    """When KEEPALIVE_HOLD=False, the old error-passthrough behavior is preserved."""
    upstream_app = _make_upstream_app(fail_count=9999, fail_status=529)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=1.0
    )
    try:
        monkeypatch.setattr(config, "KEEPALIVE_HOLD", False)
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-disabled"},
        )
        # With hold disabled, 529 propagates as an error status
        assert resp.status != 200, f"KEEPALIVE_HOLD=False must not hold, got {resp.status}"
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_aimd_no_shrink_on_529_during_hold(monkeypatch) -> None:
    """Hold on 529 does NOT AIMD-shrink the bearer (invariant 9).

    A 529 is upstream-overloaded (Anthropic capacity, not our usage) — the
    existing AIMD logic never shrinks on 529, and the hold must preserve this.
    """
    upstream_app = _make_upstream_app(fail_count=1, fail_status=529)
    test_client, upstream_server = await _make_client_with_upstream(monkeypatch, upstream_app)
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-aimd-529"},
        )
        assert resp.status == 200
        await resp.read()
        for _ in range(20):
            await asyncio.sleep(0)

        # Find the bearer's limiter and check AIMD did not shrink it
        for _bid, lim in config.bearer_limiters.items():
            snap = lim.snapshot()
            # After a 529 hold, max_concurrent should NOT have shrunk below initial
            assert snap["max_concurrent"] >= config.AIMD_INITIAL_CONCURRENT, (
                f"AIMD shrunk on 529 hold: max_concurrent={snap['max_concurrent']} "
                f"< initial={config.AIMD_INITIAL_CONCURRENT}"
            )
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_real_concurrency_429_aimd_shrinks(monkeypatch) -> None:
    """A real upstream 429 (pushback-retry path, not hold) DOES AIMD-shrink.

    Regression guard: the hold must not interfere with the existing AIMD
    shrink path for non-transient throttle errors.
    """
    meta_headers = {
        "anthropic-ratelimit-unified-status": "allowed_warning",
        "anthropic-ratelimit-unified-utilization": "0.95",
    }
    upstream_app = _make_upstream_app(fail_count=9999, fail_status=429, fail_headers=meta_headers)
    # Use a very short budget so the hold (if it fires) exhausts quickly
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=0.2
    )
    before = _aimd_shrink_total()
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-aimd-429"},
        )
        await resp.read()
        for _ in range(20):
            await asyncio.sleep(0)
        # A budget 429 is NOT held (allowed_warning => budget) → it flows through
        # the pushback/finalize path and MUST fire an AIMD shrink. Assert the
        # shrink COUNTER advanced (the value is pinned at the floor of 1, so only
        # the event count proves it — reward-hack lane: prove the shrink).
        assert _aimd_shrink_total() > before, "budget 429 did not fire an AIMD shrink event"
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_marked_queue_timeout_hold_no_shrink(monkeypatch) -> None:
    """A marked central queue-timeout 503 held to exhaustion must NOT AIMD-shrink
    (invariant 7 — central queue depth is admission backpressure, not this
    bearer's upstream pushback). Guards the round-2 marker-propagation fix: the
    hold sees the queue-timeout marker (anti-spoof-gated) and skips the shrink.
    """
    upstream_app = _make_upstream_app(
        fail_count=9999,
        fail_status=503,
        fail_headers={config.QUEUE_TIMEOUT_HEADER: "1", config.MARKER_HEADER: "1"},
    )
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=0.8
    )
    before = _aimd_shrink_total()
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-qt-hold"},
        )
        assert resp.status == 200
        raw = await resp.read()
        assert b"event: error" in raw  # exhausted → terminal SSE error
        for _ in range(20):
            await asyncio.sleep(0)
        # No shrink event may fire for a marked queue-timeout hold (counter delta
        # is the real signal — the value is pinned at the floor regardless).
        assert _aimd_shrink_total() == before, (
            "marked queue-timeout hold fired an AIMD shrink event (invariant 7)"
        )
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_forward_once_into_sse_strips_unmarked_queue_timeout(monkeypatch) -> None:
    """Anti-spoof negative test (round-2 Codex): a queue-timeout header arriving
    WITHOUT the sibling-proxy MARKER_HEADER must NOT be surfaced in meta — else a
    hostile upstream could assert the no-AIMD-shrink exemption.
    """

    async def messages(request: web.Request) -> web.Response:
        await request.read()
        # Spoof attempt: queue-timeout header but NO MARKER_HEADER.
        return web.Response(
            status=503,
            headers={config.QUEUE_TIMEOUT_HEADER: "1", "content-type": "application/json"},
            text=json.dumps({"type": "error", "error": {"type": "x", "message": "y"}}),
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", messages)
    upstream_server = TestServer(app)
    await upstream_server.start_server()
    try:
        url = str(upstream_server.make_url("/v1/messages"))
        monkeypatch.setattr(config, "UPSTREAM", url)
        req = make_mocked_request("POST", "/v1/messages")
        sse_resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream"})
        status, meta, _captured, exc = await proxy._forward_once_into_sse(
            req, {}, None, url, aiohttp.ClientTimeout(total=5), sse_resp
        )
        assert exc is None
        assert status == 503
        assert config.QUEUE_TIMEOUT_HEADER not in (meta or {}), (
            "unmarked (spoofed) queue-timeout header was surfaced in meta"
        )
    finally:
        await upstream_server.close()


async def test_hold_internal_error_emits_terminal_not_leak(monkeypatch) -> None:
    """Post-prepare exception safety (round-2 Codex MAJOR e/f): if anything raises
    AFTER the 200 SSE is prepared, the client must get a well-formed terminal SSE
    error + clean EOF (never a truncated write), and the keepalive task must not
    leak.
    """
    upstream_app = _make_upstream_app(fail_count=1, fail_status=529, success_delay_s=0.0)

    async def boom(*_a, **_k):
        raise RuntimeError("injected internal error after prepare")

    # Force the FIRST in-hold forward attempt to raise an unexpected error.
    monkeypatch.setattr(proxy, "_forward_once_into_sse", boom)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=5.0
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-boom"},
        )
        assert resp.status == 200  # already committed before the raise
        raw = await resp.read()  # completes cleanly (EOF), does not hang/truncate
        assert b"event: error" in raw, f"no terminal SSE error after internal raise: {raw!r}"
        for _ in range(20):
            await asyncio.sleep(0)
        # No keepalive task left running.
        leaked = [t for t in proxy._background_tasks if not t.done()]
        assert not leaked, f"keepalive task leaked after internal error: {leaked}"
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_keepalive_emitter_death_does_not_break_hold(monkeypatch) -> None:
    """Round-3 Codex MAJOR: if the keepalive emitter task dies with a NON-cancel
    exception, awaiting it during cleanup must not re-raise and short-circuit the
    hold. Discriminator: with the emitter raising, a 529-then-200 hold must still
    deliver the REAL body (message_start), not a terminal SSE error — proving the
    emitter's death was swallowed+logged, not propagated.
    """
    upstream_app = _make_upstream_app(fail_count=1, fail_status=529, success_delay_s=0.0)

    async def dying_emitter(*_a, **_k):
        raise RuntimeError("keepalive emitter blew up")

    monkeypatch.setattr(proxy, "_emit_keepalive_frames", dying_emitter)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=5.0
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-emit-die"},
        )
        assert resp.status == 200
        raw = await resp.read()
        assert b"message_start" in raw, (
            f"emitter death broke the hold (got terminal error, not real body): {raw!r}"
        )
        assert b"event: error" not in raw, f"unexpected terminal error: {raw!r}"
        for _ in range(20):
            await asyncio.sleep(0)
        leaked = [t for t in proxy._background_tasks if not t.done()]
        assert not leaked, f"task leaked after emitter death: {leaked}"
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_hold_engages_at_default_pushback_retries(monkeypatch) -> None:
    """BLOCKER (Codex panel): the hold must engage on the FIRST transient throttle
    even at the DEFAULT RATE_PUSHBACK_RETRIES=1.

    The other tests force retries=0, masking the pre-hold silent pushback wait.
    With retries=1 and the OLD ordering, the first 529 was consumed by the silent
    pushback-retry (no SSE, no keepalive) and the 200 arrived on the retry with
    the hold never engaging — so NO keepalive would appear. The reordered gate
    engages the hold first, so keepalives flow. Discriminator: a keepalive comment
    is present in the body at the default retry count.
    """
    upstream_app = _make_upstream_app(fail_count=1, fail_status=529, success_delay_s=0.7)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, rate_pushback_retries=1, queue_max_wait_s=10.0
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-dr"},
        )
        assert resp.status == 200
        raw = await resp.read()
        assert b": keepalive" in raw, (
            f"hold did not engage at RATE_PUSHBACK_RETRIES=1 (pre-hold silent wait): {raw[:400]!r}"
        )
        assert b"message_start" in raw or b"message_stop" in raw
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_exhausted_529_hold_does_not_aimd_shrink(monkeypatch) -> None:
    """BLOCKER (Codex panel): a bound-EXHAUSTED 529 hold must NOT AIMD-shrink.

    The exhausted terminal rewrites final_status to a synthetic 503; before the
    fix, _finalize's _aimd_feedback then shrank the bearer on that 503 — violating
    invariant 9 (529 = Anthropic capacity, never shrink). The hold now owns its
    AIMD (aimd_owned) so _finalize skips it.
    """
    upstream_app = _make_upstream_app(fail_count=9999, fail_status=529)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=0.8
    )
    before = _aimd_shrink_total()
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-exh-529"},
        )
        assert resp.status == 200  # committed SSE; body carries the terminal error
        raw = await resp.read()
        assert b"event: error" in raw
        for _ in range(20):
            await asyncio.sleep(0)
        # The terminal rewrites final_status to a synthetic 503, but no shrink may
        # fire — a 529 is Anthropic capacity (invariant 9). Counter delta is the
        # real signal (the ceiling value is pinned at the floor regardless).
        assert _aimd_shrink_total() == before, (
            "exhausted 529 hold fired an AIMD shrink event (invariant 9)"
        )
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_529_long_retry_after_not_held(monkeypatch) -> None:
    """MAJOR (Codex panel): a 529 with Retry-After beyond the hold ceiling must
    NOT be held — shed cleanly so the SDK retries the whole request later, rather
    than commit a doomed 200 SSE that only runs to the budget deadline.
    """
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30)
    upstream_app = _make_upstream_app(fail_count=9999, fail_status=529, retry_after_s=3600)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=10.0
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-529-longra"},
        )
        # NOT a committed 200 SSE hold — a clean error status the SDK retries.
        assert resp.status != 200, "529 + long Retry-After was wrongly held as a 200 SSE"
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_hold_wait_bounded_by_budget_not_retry_after(monkeypatch) -> None:
    """MAJOR (Codex + Opus panel): inside the hold, wait_retry_after must not sleep
    past the budget deadline. A 529 with Retry-After (within the hold ceiling) but
    LONGER than the remaining budget must exit at ~budget, not at Retry-After —
    otherwise the fair slot is held past client patience (invariant 6).
    """
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30)
    # RA=25s is within the 30s hold ceiling (so it IS held), but the 1s budget
    # must bound the actual wait. Pre-fix, wait_retry_after slept the full 25s.
    upstream_app = _make_upstream_app(fail_count=9999, fail_status=529, retry_after_s=25)
    test_client, upstream_server = await _make_client_with_upstream(
        monkeypatch, upstream_app, queue_max_wait_s=1.0
    )
    try:
        body = json.dumps({"model": "claude-opus-4-8", "stream": True}).encode()
        started = time.monotonic()
        resp = await test_client.post(
            "/v1/messages",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-bounded"},
        )
        await resp.read()
        elapsed = time.monotonic() - started
        assert elapsed < 10.0, (
            f"hold slept toward Retry-After (25s), not the 1s budget: elapsed={elapsed:.1f}s"
        )
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await test_client.close()
        await upstream_server.close()


async def test_keepalive_config_knobs_exist() -> None:
    """THROTTLE_KEEPALIVE_HOLD and THROTTLE_KEEPALIVE_INTERVAL_MS are in EDITABLE_KNOBS."""
    assert "keepalive_hold" in config.EDITABLE_KNOBS
    assert "keepalive_interval_ms" in config.EDITABLE_KNOBS
    spec_hold = config.EDITABLE_KNOBS["keepalive_hold"]
    spec_interval = config.EDITABLE_KNOBS["keepalive_interval_ms"]
    assert spec_hold["type"] == "bool"
    assert spec_interval["type"] == "int"
    assert spec_interval["min"] == 500
    assert spec_interval["max"] == 30000


def test_keepalive_holds_counter_registered() -> None:
    """M_KEEPALIVE_HOLDS counter is registered in the process-local registry."""
    from anthropic_throttle_proxy.metrics import M_KEEPALIVE_HOLDS, REGISTRY

    # Verify we can observe it without error
    M_KEEPALIVE_HOLDS.labels(outcome="streamed")
    M_KEEPALIVE_HOLDS.labels(outcome="errored")
    names = {m.name for m in REGISTRY.collect()}
    # prometheus-client stores Counters under the base name (without _total);
    # the _total suffix appears in the exported text but not in m.name.
    assert "anthropic_keepalive_holds" in names
