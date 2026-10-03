"""Real-runtime integration for the T004 ingress seam (no stubs).

The actual 297 ``ProspectiveRuntime`` is attached at ``RUNTIME_KEY`` exactly as
the coordinator will, and the actual ``ingress._send_upstream`` seam runs
against a loopback aiohttp upstream that counts hits and captures bytes.

Contract under test: ``ingress_relay`` is UNSUPPORTED in strict — the typed
LOCAL 503 is emitted before transport with zero upstream hits; observe allows
the traffic and classifies it ``unknown``; off (the shared default runtime)
forwards the actual bytes untouched. Validation runs on the Mac coordinator's
side (no desktop tests/network/provider calls).
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request

from anthropic_throttle_proxy import ingress
from anthropic_throttle_proxy.prospective_admission import Budgets, PersistenceOwner, Scope
from anthropic_throttle_proxy.prospective_refusal import (
    ERROR_TYPE,
    PROVENANCE_SOURCE_HEADER,
)
from anthropic_throttle_proxy.prospective_runtime import RUNTIME_KEY, ProspectiveRuntime
from anthropic_throttle_proxy.prospective_scope import ScopeResolver

BODY = b'{"model":"canonical-model","messages":[{"role":"user","content":"t"}]}'
_UPSTREAM = "http://upstream.invalid/v1"
_ACCOUNT = "account-a"
_MODEL = "canonical-model"


def _runtime(tmp_path, mode: str) -> ProspectiveRuntime:
    """One real enabled runtime: explicit resolver, custody scope and bounds."""
    budgets = Budgets(max_requests=4, max_tokens=10000)
    resolver = ScopeResolver(
        [
            {
                "source": "label-a",
                "endpoint": _UPSTREAM,
                "alias": "model-alias",
                "upstream": _ACCOUNT,
                "account": _ACCOUNT,
                "model": _MODEL,
                "budgets": {"max_requests": 4, "max_tokens": 10000},
                "output_default": 256,
            }
        ],
        sources={"label-a"},
        endpoints={_UPSTREAM},
        models={"model-alias", _MODEL},
    )
    custody = Scope(
        key=(_ACCOUNT, _ACCOUNT, _MODEL),
        budgets=budgets,
        state_path=str(tmp_path / "prospective-state.json"),
        allow_cold_start=True,
    )
    return ProspectiveRuntime(
        mode=mode,
        resolver=resolver,
        scopes=(custody,),
        max_pending=2,
        wait_timeout=1.0,
        budget_label="fleet",
        retry_after_s=5,
    )


async def _loopback(hits: list[bytes]) -> tuple[TestServer, str]:
    async def handler(request: web.Request) -> web.Response:
        hits.append(await request.read())
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_post("/v1/messages", handler)
    server = TestServer(app)
    await server.start_server()
    return server, str(server.make_url("/v1/messages"))


def _request(runtime) -> web.Request:
    app = web.Application()
    app[RUNTIME_KEY] = runtime
    return make_mocked_request("POST", "/v1/messages", app=app)


async def _send(request: web.Request, target: str) -> web.Response:
    async with aiohttp.ClientSession() as session:
        result = await ingress._send_upstream(
            session,
            request,
            target,
            BODY,
            aiohttp.ClientTimeout(total=5),
            None,
        )
        if isinstance(result, aiohttp.ClientResponse):
            await result.read()
        return result


async def test_strict_ingress_relay_is_local_503_with_zero_upstream_hits(tmp_path):
    hits: list[bytes] = []
    server, target = await _loopback(hits)
    runtime = _runtime(tmp_path, "strict")
    await runtime.start()
    try:
        response = await _send(_request(runtime), target)
        assert response.status == 503
        assert ERROR_TYPE in response.text
        assert response.headers.get(PROVENANCE_SOURCE_HEADER) == "local"
        # Unsupported topology refuses BEFORE transport: the loopback never
        # sees a byte, and no provider-facing retry/spill path is entered.
        assert hits == []
    finally:
        await runtime.aclose()
        await server.close()


@pytest.mark.parametrize("mode", ["observe", "off"])
async def test_observe_and_off_forward_actual_bytes(tmp_path, mode):
    hits: list[bytes] = []
    server, target = await _loopback(hits)
    runtime = _runtime(tmp_path, "observe") if mode == "observe" else None
    if runtime is not None:
        await runtime.start()
    try:
        # off control attaches NO runtime: get_runtime serves the shared OFF
        # default and the seam forwards the exact final bytes unchanged.
        request = (
            _request(runtime)
            if runtime is not None
            else make_mocked_request("POST", "/v1/messages", app=web.Application())
        )
        response = await _send(request, target)
        assert response.status == 200
        assert await response.text() == "ok"
        assert hits == [BODY]
    finally:
        await server.close()
        if runtime is not None:
            await runtime.aclose()
    if runtime is not None:
        # Observe classified the unsupported topology UNKNOWN and sent.
        assert runtime.observations()["unknown"] == 1


async def test_persistence_owner_is_not_instantiated_by_the_off_path():
    # The shared OFF default allocates nothing: no owner, no state file.
    request = make_mocked_request("POST", "/v1/messages", app=web.Application())
    runtime = ingress._prospective_bridge().get_runtime(request)
    assert runtime.mode == "off"
    assert not isinstance(getattr(runtime, "_owner", None), PersistenceOwner)
