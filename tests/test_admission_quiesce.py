"""Race-free admission quiesce: close -> drain -> swap -> reopen (issue #300).

Covers the exact gap (a request arriving between quiesce and the stop is refused
while earlier work completes) AND Root's falsifier: a request that passed the
gate but is still uploading its body must be counted (`admitted_holds`) from the
first instant until completion — including cancel/error paths — so close-and-drain
can never observe a false zero.

Reuses the ``test_keepalive_hold`` client harness (real ``proxy.handler`` app);
loopback stub upstream only. ``quiesce``/``resume``/``admission`` are invoked as
their route handlers directly — ``main()`` owns route registration.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from aiohttp import web
from test_keepalive_hold import _make_client_with_upstream

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.ui import routes as ui_routes

_BODY = json.dumps({"model": "claude-opus-4-8", "max_tokens": 16, "messages": []}).encode()
_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer test-quiesce"}


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


@asynccontextmanager
async def _scenario(monkeypatch, upstream_url: str | None = None):
    """Real proxy.handler client + counting stub; deterministic gate/hold state."""
    upstream = _CountingUpstream()
    upstream.release.set()
    up_app = web.Application()
    up_app.router.add_route("*", "/{prefix:.*}v1/messages", upstream.messages)
    up_app.router.add_route("*", "/v1/messages", upstream.messages)
    up_app.router.add_route("*", "/{path:.*}", upstream.messages)
    # Register the real control routes exactly as main() does, via the harness's
    # attach_ui seam (no harness duplication): closure/resume are exercised as
    # ACTUAL control HTTP POSTs.
    real_attach = ui_routes.attach_ui

    def attach_with_control(app):
        real_attach(app)
        app.router.add_post("/__throttle/quiesce", proxy.quiesce)
        app.router.add_post("/__throttle/resume", proxy.resume)
        app.router.add_get("/__throttle/admission", proxy.admission)

    monkeypatch.setattr("test_keepalive_hold.attach_ui", attach_with_control)
    client, up_server = await _make_client_with_upstream(monkeypatch, up_app)
    if upstream_url is not None:
        monkeypatch.setattr(config, "UPSTREAM", upstream_url)
    await proxy.resume(None)
    config.state["admitted_holds"] = 0
    try:
        yield client, upstream
    finally:
        await client.close()
        await up_server.close()


def _stalled_body(stall: asyncio.Event):
    """Async body that passes the gate, then hangs mid-upload until released."""

    async def gen():
        yield b'{"model":"claude-opus-4-8","messages":['
        await stall.wait()
        yield b"]}"

    return gen()


def _drained() -> bool:
    return (
        config.state["inflight"] == 0
        and config.state["queued"] == 0
        and config.state["keepalive_holds_active"] == 0
        and config.state["admitted_holds"] == 0
    )


async def _settle():
    """Let the server-side guaranteed finally unwind before asserting."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _stalled_request(client, stall: asyncio.Event):
    """Start a body-stalled request and let it reach the false-zero window."""
    task = asyncio.create_task(
        client.post("/v1/messages", data=_stalled_body(stall), headers=_HEADERS)
    )
    for _ in range(20):
        await asyncio.sleep(0)
    return task


async def _refused(client):
    """A post-gate-refused request: 503 marker, fully consumed."""
    response = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
    await response.read()
    assert response.status == 503
    return response


async def test_slow_upload_past_gate_counts_until_completion(monkeypatch):
    """Root's falsifier: gate-passed + body-stalled request must NOT read as
    drained while it exists, and must release exactly once on completion."""
    async with _scenario(monkeypatch) as (client, upstream):
        stall = asyncio.Event()
        task = await _stalled_request(client, stall)
        # Past the gate, still uploading: nothing else tracks it yet — the
        # admission hold is the only nonzero signal (the false-zero window).
        assert config.state["admitted_holds"] == 1
        assert config.state["inflight"] == 0 and config.state["queued"] == 0

        await proxy.quiesce(None)
        assert not _drained(), "quiesce must never see drained while an admitted request lives"

        stall.set()
        response = await asyncio.wait_for(task, timeout=5)
        await response.read()
        assert response.status == 200
        await _settle()
        assert config.state["admitted_holds"] == 0
        assert _drained()


async def test_cancelled_upload_releases_admission_hold(monkeypatch):
    async with _scenario(monkeypatch) as (client, upstream):
        stall = asyncio.Event()
        task = await _stalled_request(client, stall)
        assert config.state["admitted_holds"] == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for _ in range(20):
            await asyncio.sleep(0)
        assert config.state["admitted_holds"] == 0, "cancellation must release the hold"
        assert _drained()
        assert upstream.hits == 0


async def test_error_path_releases_admission_hold(monkeypatch):
    """A request that dies on an upstream error also releases exactly once."""
    async with _scenario(monkeypatch, upstream_url="http://127.0.0.1:1") as (client, _upstream):
        response = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await response.read()
        assert response.status >= 400
        await _settle()
        assert config.state["admitted_holds"] == 0, "error path must release the hold"
        assert _drained()


async def test_request_in_the_quiesce_gap_is_refused_and_inflight_completes(monkeypatch):
    """THE gap test: a new request arrives after quiesce and before any stop —
    refused at the gate (zero upstream) while the in-flight request completes;
    the drained state (holds included) stays closed."""
    async with _scenario(monkeypatch) as (client, upstream):
        upstream.release.clear()
        inflight = asyncio.create_task(client.post("/v1/messages", data=_BODY, headers=_HEADERS))
        await asyncio.wait_for(upstream.entered.wait(), timeout=2)
        assert upstream.hits == 1

        closed = await proxy.quiesce(None)
        assert json.loads(closed.body)["admission"] == "closed"

        gap = await _refused(client)
        assert gap.headers.get("x-throttle-admission-closed") == "1"
        assert upstream.hits == 1, "gap request must never reach upstream"

        upstream.release.set()
        response = await asyncio.wait_for(inflight, timeout=5)
        await response.read()
        assert response.status == 200

        for _ in range(20):
            await asyncio.sleep(0)
        assert _drained(), "drain must include admitted_holds"
        await _refused(client)
        assert upstream.hits == 1

        opened = await proxy.resume(None)
        assert json.loads(opened.body)["admission"] == "open"
        ok = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await ok.read()
        assert ok.status == 200
        assert upstream.hits == 2


async def test_pipelined_arrival_on_live_connection_refused_while_stream_held(monkeypatch):
    """REAL-HTTP barrier falsifier (CLOSURE-2640/ROOT-1315).

    Same persistent TCP connection: an admitted request's response is held
    upstream; during the barrier a pipelined arrival on that live connection
    must be refused at request level (503 marker) with ZERO new upstream
    dispatch. Closure/resume go through actual control HTTP POSTs; a cancelled
    slow upload releases its admission hold exactly once; resume is the
    negative control (traffic flows again).
    """
    async with _scenario(monkeypatch) as (client, upstream):
        upstream.release.clear()
        url = client.make_url("/")
        reader, writer = await asyncio.open_connection(str(url.host), url.port)

        def request() -> bytes:
            return (
                b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-quiesce\r\n"
                b"Content-Type: application/json\r\nContent-Length: "
                + str(len(_BODY)).encode()
                + b"\r\n\r\n"
                + _BODY
            )

        async def read_response():
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            status = head.split(b"\r\n", 1)[0]
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            if b"transfer-encoding: chunked" in head.lower():
                # The proxy streams responses back; consume the chunked frame
                # so the next response starts at a clean boundary.
                while True:
                    size_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=5)
                    size = int(size_line.strip() or b"0", 16)
                    await asyncio.wait_for(reader.readexactly(size + 2), timeout=5)
                    if size == 0:
                        break
            elif length:
                await asyncio.wait_for(reader.readexactly(length), timeout=5)
            return status, head.lower(), b""

        try:
            writer.write(request())
            await writer.drain()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if upstream.entered.is_set() and config.state["admitted_holds"] == 1:
                    break
            assert upstream.entered.is_set() and upstream.hits == 1

            closed = await client.post("/__throttle/quiesce")
            assert (await closed.json())["admission"] == "closed"

            # Pipelined arrival on the SAME live connection during the barrier.
            writer.write(request())
            await writer.drain()
            assert upstream.hits == 1, "barrier bytes must not dispatch upstream"

            upstream.release.set()
            status1, _head1, _body1 = await read_response()
            assert b"200" in status1
            status2, head2, _body2 = await read_response()
            assert b"503" in status2
            assert b"x-throttle-admission-closed: 1" in head2
            assert upstream.hits == 1, "the refused arrival never dispatches"
        finally:
            writer.close()

        # Reopen first: the exactly-once release semantics apply to ADMITTED
        # requests, which only exist while the gate is open.
        opened = await client.post("/__throttle/resume")
        assert (await opened.json())["admission"] == "open"

        # Cancellation releases the admission hold EXACTLY once.
        for _ in range(2):
            stall = asyncio.Event()
            task = asyncio.create_task(
                client.post("/v1/messages", data=_stalled_body(stall), headers=_HEADERS)
            )
            for _ in range(100):
                await asyncio.sleep(0.01)
                if config.state["admitted_holds"] == 1:
                    break
            assert config.state["admitted_holds"] == 1
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await _settle()
            assert config.state["admitted_holds"] == 0, "release must happen exactly once"

        # Negative control: traffic flows normally after resume.
        ok = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await ok.read()
        assert ok.status == 200
        assert upstream.hits == 2


async def test_quiesce_keeps_control_surfaces_answerable(monkeypatch):
    async with _scenario(monkeypatch) as (client, upstream):
        await proxy.quiesce(None)
        health = await client.get("/__throttle/health")
        assert health.status == 200
        assert "admitted_holds" in (await health.json())
        admission = await proxy.admission(None)
        assert admission.status == 200
        await _refused(client)
        assert config.state["admitted_holds"] == 0, "refused requests are never counted"
        assert upstream.hits == 0


async def test_quiesce_and_resume_are_idempotent(monkeypatch):
    async with _scenario(monkeypatch) as (client, _upstream):
        for _ in range(2):
            assert json.loads((await proxy.quiesce(None)).body)["admission"] == "closed"
        await _refused(client)
        for _ in range(2):
            assert json.loads((await proxy.resume(None)).body)["admission"] == "open"
        ok = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
        await ok.read()
        assert ok.status == 200
