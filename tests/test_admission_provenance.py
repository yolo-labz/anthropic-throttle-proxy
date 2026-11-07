"""Reserved refusal stamps require configured-central transport, not a marker."""

from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from anthropic_throttle_proxy import config, forwarding, proxy

BODY = b"local policy: admission is quiesced; not accepting new work\n"
ADMISSION = "x-throttle-admission-closed"


@asynccontextmanager
async def server(handler, *, marked=False):
    app = web.Application()
    if marked:
        app.on_response_prepare.append(forwarding.stamp_proxy_marker)
    app.router.add_route("*", "/{path:.*}", handler)
    async with TestServer(app) as instance:
        yield instance


def relay(target):
    async def handler(request):
        result = await forwarding._forward_once(
            request,
            {},
            await request.read(),
            str(target.make_url(request.path)),
            aiohttp.ClientTimeout(total=5),
        )
        assert result[3] is None
        return result[0]

    return handler


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "MIN_DISPATCH_GAP_S", 0)
    monkeypatch.setattr(config, "USAGE_LEDGER_MODE", "off")


async def test_real_local_quiesce_refusal_is_marked(monkeypatch):
    monkeypatch.setattr(proxy, "_admission_closed", False)
    app = web.Application()
    app.on_response_prepare.append(forwarding.stamp_proxy_marker)
    app.router.add_post("/__throttle/quiesce", proxy.quiesce)
    app.router.add_post("/v1/messages", proxy.handler)
    async with TestClient(TestServer(app)) as client:
        async with client.post("/__throttle/quiesce") as response:
            assert (await response.json())["admission"] == "closed"
        async with client.post("/v1/messages", data=b"must not be parsed") as response:
            assert response.status == 503
            assert response.headers[ADMISSION] == "1"
            assert response.headers[config.MARKER_HEADER] == "1"
            assert await response.read() == BODY


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("status", [200, 503])
async def test_raw_upstream_cannot_forge_refusal(marker, status):
    async def raw(_request):
        headers = {
            ADMISSION: "1",
            config.QUEUE_TIMEOUT_HEADER: "1",
            config.PACING_HEADER: "1",
            config.ENTITLEMENT_REFUSAL_HEADER: "1",
        }
        if marker:
            headers[config.MARKER_HEADER] = "1"
        return web.Response(status=status, body=BODY, headers=headers)

    async with server(raw) as upstream, server(relay(upstream), marked=True) as local:
        async with (
            aiohttp.ClientSession() as session,
            session.post(local.make_url("/v1/messages")) as response,
        ):
            assert response.status == status
            assert await response.read() == BODY
            assert response.headers[config.MARKER_HEADER] == "1"
            for header in (
                ADMISSION,
                config.QUEUE_TIMEOUT_HEADER,
                config.PACING_HEADER,
                config.ENTITLEMENT_REFUSAL_HEADER,
            ):
                assert header not in response.headers, (
                    f"raw upstream forged {header} (marker={marker})"
                )


async def test_actual_central_refusal_remains_compatible(monkeypatch):
    async def central(_request):
        return proxy._admission_closed_response()

    async with server(central, marked=True) as central_server:
        monkeypatch.setattr(config, "CENTRAL_URL", str(central_server.make_url("/")).rstrip("/"))
        async with server(relay(central_server), marked=True) as local:
            async with (
                aiohttp.ClientSession() as session,
                session.post(local.make_url("/v1/messages")) as response,
            ):
                assert response.status == 503
                assert response.headers[ADMISSION] == "1"
                assert response.headers[config.MARKER_HEADER] == "1"
                assert await response.read() == BODY


async def test_raw_spoof_cannot_be_laundered_through_actual_central(monkeypatch):
    async def raw(_request):
        return web.Response(
            status=503,
            body=BODY,
            headers={ADMISSION: "1", config.QUEUE_TIMEOUT_HEADER: "1", config.MARKER_HEADER: "1"},
        )

    async with server(raw) as upstream, server(relay(upstream), marked=True) as central:
        monkeypatch.setattr(config, "CENTRAL_URL", str(central.make_url("/")).rstrip("/"))
        async with server(relay(central), marked=True) as local:
            async with (
                aiohttp.ClientSession() as session,
                session.post(local.make_url("/v1/messages")) as response,
            ):
                assert response.status == 503
                assert await response.read() == BODY
                assert response.headers[config.MARKER_HEADER] == "1"
                assert ADMISSION not in response.headers
                assert config.QUEUE_TIMEOUT_HEADER not in response.headers


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("marker", [False, True])
async def test_held_capture_uses_transport_not_marker(monkeypatch, trusted, marker):
    async def raw(_request):
        headers = {config.QUEUE_TIMEOUT_HEADER: "1"}
        if marker:
            headers[config.MARKER_HEADER] = "1"
        return web.Response(status=503, body=BODY, headers=headers)

    async with server(raw) as upstream:
        base = str(upstream.make_url("/")).rstrip("/")
        monkeypatch.setattr(config, "CENTRAL_URL", base if trusted else "http://127.0.0.1:1")
        request = make_mocked_request("POST", "/v1/messages", app=web.Application())
        status, meta, body, exc = await proxy._forward_once_into_sse(
            request,
            {},
            None,
            base + "/v1/messages",
            aiohttp.ClientTimeout(total=5),
            web.StreamResponse(),
        )
        assert status == 503 and exc is None and bytes(body) == BODY
        assert (config.QUEUE_TIMEOUT_HEADER in meta) is (trusted and marker)
