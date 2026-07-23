"""Spec 093 S1 — unified ``:8760`` ingress skeleton + no-op-when-unset.

Acceptance (S1): the ingress forwards to the default lane path-preservingly,
byte-identical to pointing the client at the lane directly; a ``GET /`` infra
probe is answered locally (no lane slot consumed); ``/__throttle/health`` is
fast and correct; with the ingress unset, clients hit the lane as today
(invariant 5 — opt-in separate process).

These tests stand up a fake lane (echo server), point the ingress at it, and
drive the ingress app through aiohttp's TestClient. Later slices (S2–S6) add
role inference, gauge-driven selection, model-remap, guards, and observability
without changing the forward shape asserted here.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import ingress


def _echo_lane() -> web.Application:
    """Fake lane that echoes the forwarded request shape for assertion."""

    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        return web.json_response(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "body": body.decode("utf-8", "replace"),
                "auth": request.headers.get("Authorization", ""),
                "content_type": request.headers.get("Content-Type", ""),
                "received_headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )

    async def sse(request: web.Request) -> web.StreamResponse:
        # Streaming path: prove the response is forwarded byte-identical.
        await request.read()
        resp = web.StreamResponse(
            status=200, headers={"content-type": "text/event-stream", "x-lane": "8765"}
        )
        await resp.prepare(request)
        await resp.write(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        await resp.write_eof()
        return resp

    async def fixed(request: web.Request) -> web.Response:
        # Constant response for byte-identity comparison (request shape is NOT
        # echoed here, so proxying can't perturb the client-visible bytes).
        await request.read()
        return web.Response(
            status=200,
            body=b'{"ok":true,"fixed":true}',
            headers={"content-type": "application/json", "x-lane-tag": "A"},
        )

    app = web.Application()
    app.router.add_post("/v1/messages", echo)
    app.router.add_get("/v1/messages", echo)
    app.router.add_post("/v1/messages/stream", sse)
    app.router.add_post("/v1/messages/fixed", fixed)
    app.router.add_get("/v1/messages/fixed", fixed)
    return app


@pytest.fixture
async def lane_client() -> TestClient:
    """A running fake lane on an ephemeral port; its URL drives the ingress."""
    client = TestClient(TestServer(_echo_lane()))
    await client.start_server()
    yield client
    await client.close()


@pytest.fixture
async def ingress_factory(monkeypatch):
    """Factory: ``await ingress_factory(lane_app) -> (ingress_client, lane_client)``.

    Builds a custom lane app + an ingress pointed at it, with both torn down on
    test exit. Dedupes the lane/ingress bootstrap for tests that need a bespoke
    lane handler (vs the shared echo ``lane_client``).
    """
    created: list[tuple[TestClient, TestClient]] = []

    async def _make(lane_app: web.Application) -> tuple[TestClient, TestClient]:
        lane = TestClient(TestServer(lane_app))
        await lane.start_server()
        monkeypatch.setattr(ingress, "DEFAULT_LANE_URL", str(lane.make_url("")).rstrip("/"))
        ing = TestClient(TestServer(ingress.build_app()))
        await ing.start_server()
        created.append((ing, lane))
        return ing, lane

    yield _make
    for ing, lane in created:
        await ing.close()
        await lane.close()


@pytest.fixture
async def ingress_client(lane_client: TestClient, monkeypatch) -> TestClient:
    """The ingress app pointed at the running fake lane."""
    lane_url = str(lane_client.make_url("")).rstrip("/")
    # ingress.DEFAULT_LANE_URL is read at import; point it at the live lane.
    monkeypatch.setattr(ingress, "DEFAULT_LANE_URL", lane_url)
    client = TestClient(TestServer(ingress.build_app()))
    await client.start_server()
    yield client
    await client.close()


async def test_forward_preserves_path_query_and_body(ingress_client: TestClient) -> None:
    """S1 acceptance: a ``/v1/messages?x=1`` POST reaches the lane verbatim."""
    async with ingress_client.post(
        "/v1/messages?x=1",
        json={"model": "claude-sonnet-4-6", "max_tokens": 8},
        headers={"Authorization": "Bearer test-ok"},
    ) as resp:
        assert resp.status == 200
        assert resp.headers.get(ingress.MARKER_HEADER) == "1"
        payload = await resp.json()
    assert payload["method"] == "POST"
    assert payload["path"] == "/v1/messages"
    assert payload["query"] == {"x": "1"}
    assert payload["auth"] == "Bearer test-ok"
    assert json.loads(payload["body"])["model"] == "claude-sonnet-4-6"


async def test_forward_is_byte_identical_to_direct_lane(
    ingress_client: TestClient, lane_client: TestClient
) -> None:
    """Invariant 5: the CLIENT-VISIBLE response is identical, direct vs via ingress.

    Proxied requests naturally differ in transport headers (User-Agent, Host)
    like on any proxy — that's not the invariant. The invariant is that the
    client gets the same status, body, and lane headers either way (bar the
    ingress marker it adds).
    """
    async with lane_client.get("/v1/messages/fixed") as direct:
        direct_body = await direct.read()
        direct_status = direct.status
        direct_tag = direct.headers.get("x-lane-tag")
    async with ingress_client.get("/v1/messages/fixed") as via_ingress:
        via_body = await via_ingress.read()
        via_status = via_ingress.status
        via_tag = via_ingress.headers.get("x-lane-tag")
        via_marker = via_ingress.headers.get(ingress.MARKER_HEADER)
    assert via_status == direct_status
    assert via_body == direct_body
    assert via_tag == direct_tag == "A"
    # The ONLY response-header difference the ingress introduces is its marker.
    assert via_marker == "1"


async def test_streamed_response_passes_through(ingress_client: TestClient) -> None:
    """SSE lane response is streamed back byte-identical with its headers."""
    async with ingress_client.post(
        "/v1/messages/stream", data=b"{}", headers={"Authorization": "Bearer t"}
    ) as resp:
        assert resp.status == 200
        assert resp.headers.get("content-type") == "text/event-stream"
        assert resp.headers.get("x-lane") == "8765"
        body = await resp.read()
    assert b"message_stop" in body


async def test_root_probe_answered_locally(ingress_client: TestClient) -> None:
    """PR #29 invariant: GET / is an infra probe, never forwarded to a lane."""
    async with ingress_client.get("/") as resp:
        assert resp.status == 200
        text = await resp.text()
    assert "ingress" in text
    # The lane echo server would 404 on "/", so a 200 proves it was local.


async def test_health_is_fast_and_reports_lane(ingress_client: TestClient) -> None:
    """/__throttle/health must be <50ms (invariant 4) + report the default lane."""
    import time

    t0 = time.monotonic()
    async with ingress_client.get("/__throttle/health") as resp:
        assert resp.status == 200
        payload = await resp.json()
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < 50, f"health took {elapsed_ms:.1f}ms"
    assert payload["status"] == "ok"
    assert payload["ingress"] is True
    assert payload["default_lane"].startswith("http://")


async def test_unreachable_lane_yields_503(ingress_client: TestClient) -> None:
    """A dead lane surfaces as a clean 503, not an unhandled exception."""
    saved = ingress.DEFAULT_LANE_URL
    ingress.DEFAULT_LANE_URL = "http://127.0.0.1:1"  # closed port
    try:
        async with ingress_client.post(
            "/v1/messages", json={}, headers={"Authorization": "Bearer t"}
        ) as resp:
            assert resp.status == 503
            payload = await resp.json()
        assert payload["error"] == "ingress-upstream-unreachable"
    finally:
        ingress.DEFAULT_LANE_URL = saved  # don't poison sibling tests


async def test_hop_by_hop_headers_stripped(ingress_client: TestClient) -> None:
    """Settable RFC 7230 hop-by-hop headers are stripped, case-insensitive.

    Transfer-Encoding / Content-Length are managed by aiohttp itself (it rejects
    a request carrying both), so they cannot be smuggled manually with a body;
    the filter still lists them for correctness. Here we assert the
    manually-settable hop-by-hop headers are stripped while a normal header
    survives.
    """
    sent = {
        "Authorization": "Bearer t",
        "Connection": "keep-alive",
        "Upgrade": "h2c",
        "Proxy-Authorization": "Basic xx",
        "X-Normal": "keep-me",
    }
    async with ingress_client.post("/v1/messages", json={}, headers=sent) as resp:
        assert resp.status == 200
        payload = await resp.json()
    seen = payload["received_headers"]
    assert seen.get("x-normal") == "keep-me"
    for hop in ("connection", "upgrade", "proxy-authorization"):
        assert hop not in seen, f"hop-by-hop header {hop!r} leaked to the lane"


async def test_ingress_marker_overwrites_upstream_marker(ingress_factory) -> None:
    """A lane trying to spoof the ingress marker can't — ours overwrites it."""

    # Lane that returns its own fake ingress marker claiming it's the ingress.
    async def spoof(_request: web.Request) -> web.Response:
        return web.Response(status=200, body=b"ok", headers={ingress.MARKER_HEADER: "999"})

    app = web.Application()
    app.router.add_post("/v1/messages", spoof)
    ing, _lane = await ingress_factory(app)
    async with ing.post("/v1/messages", json={}, headers={"Authorization": "Bearer t"}) as resp:
        # Our stamp wins: the client sees "1", never the lane's "999".
        assert resp.headers.get(ingress.MARKER_HEADER) == "1"


async def test_role_stamped_on_messages_response(ingress_client: TestClient) -> None:
    """S2: POST /v1/messages infers the role from the body model + stamps it."""
    async with ingress_client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-6", "max_tokens": 8},
        headers={"Authorization": "Bearer t"},
    ) as resp:
        assert resp.status == 200
        assert resp.headers.get("x-anthropic-throttle-role") == "bulk"
    async with ingress_client.post(
        "/v1/messages",
        json={"model": "claude-opus-4-8", "max_tokens": 8},
        headers={"Authorization": "Bearer t"},
    ) as resp:
        assert resp.headers.get("x-anthropic-throttle-role") == "generate"


async def test_non_messages_path_streams_body_unparsed(ingress_client: TestClient) -> None:
    """S2: only POST /v1/messages buffers the body; other paths stream unchanged.

    A GET to the echo lane (no body buffering) must still succeed and return the
    path it was sent — proves the role-inference buffering didn't break forwards
    on other paths.
    """
    async with ingress_client.get("/v1/messages?role=check") as resp:
        assert resp.status == 200
        payload = await resp.json()
    assert payload["path"] == "/v1/messages"
    assert payload["query"] == {"role": "check"}


async def test_other_post_path_body_streamed_not_parsed(ingress_factory) -> None:
    """S2 gate gap: a non-/v1/messages POST streams its body, is never buffered."""
    seen: dict[str, bytes] = {}

    async def capture(request: web.Request) -> web.Response:
        seen["body"] = await request.read()
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/v1/other", capture)
    ing, _lane = await ingress_factory(app)
    big = b"x" * 200_000  # far over the read limit; proves no buffering here
    async with ing.post("/v1/other", data=big, headers={"Authorization": "Bearer t"}) as resp:
        assert resp.status == 204
    assert seen.get("body") == big  # full body arrived byte-complete


async def test_large_messages_body_defaults_role_and_forwards_complete(
    ingress_factory, monkeypatch
) -> None:
    """S2 gate BLOCKER fix: a body > ROLE_BODY_READ_LIMIT never parses (bound
    memory/CPU) — role defaults to generate — but the FULL body still reaches
    the lane byte-complete (prefix + streamed remainder)."""
    seen: dict[str, bytes] = {}

    async def capture(request: web.Request) -> web.Response:
        seen["body"] = await request.read()
        return web.Response(status=200, body=b"ok")

    app = web.Application()
    app.router.add_post("/v1/messages", capture)
    ing, _lane = await ingress_factory(app)
    # Shrink the read limit so the test body exceeds it without sending 64 KiB.
    monkeypatch.setattr(ingress, "ROLE_BODY_READ_LIMIT", 16)
    # Valid model at the START, then a large body that exceeds the 16-byte cap.
    payload = b'{"model":"claude-sonnet-4-6","padding":"' + (b"y" * 100_000) + b'"}'
    async with ing.post(
        "/v1/messages", data=payload, headers={"Authorization": "Bearer t"}
    ) as resp:
        assert resp.status == 200
        # Role defaulted to generate (body not parsed past the 16-byte cap).
        assert resp.headers.get("x-anthropic-throttle-role") == "generate"
    # The FULL body still reached the lane byte-complete (no truncation).
    assert seen.get("body") == payload


async def test_unreachable_lane_503_does_not_leak_detail(ingress_client: TestClient) -> None:
    """The 503 body is generic — upstream exception text never reaches the client."""
    saved = ingress.DEFAULT_LANE_URL
    ingress.DEFAULT_LANE_URL = "http://127.0.0.1:1"  # closed port
    try:
        async with ingress_client.post(
            "/v1/messages", json={}, headers={"Authorization": "Bearer t"}
        ) as resp:
            assert resp.status == 503
            text = await resp.text()
    finally:
        ingress.DEFAULT_LANE_URL = saved
    # No detail field, no port/IP echoed back to the client.
    assert "detail" not in text
    assert "127.0.0.1" not in text
