"""Focused T004 ingress-seam tests — synthetic stubs only; no live traffic.

Scope: the ``_send_upstream`` prospective seam and the hop-boundary provenance
strip. The 297 runtime bridge is stubbed at its published accessor surface
(``prospective_runtime``); the merged ``prospective_refusal`` module is the
real one. Validation runs on the Mac coordinator's side (no desktop runs).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import aiohttp
from aiohttp.test_utils import make_mocked_request

from anthropic_throttle_proxy import ingress
from anthropic_throttle_proxy.prospective_refusal import (
    ERROR_TYPE,
    ProspectiveRefusal,
    RefusalReason,
    strip_incoming_provenance,
)

BODY = b'{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"t"}]}'
TARGET = "http://127.0.0.1:1/v1/messages"


class _StubPermit:
    def __init__(self, log):
        self._log = log

    def handoff(self):
        self._log.append("handoff")


class LocalProspectiveRefusal(Exception):
    """Stand-in for prospective_runtime.LocalProspectiveRefusal (297 API)."""

    def __init__(self):
        self.refusal = ProspectiveRefusal(
            reason=RefusalReason.UNKNOWN, retry_after_s=30, budget_label="fleet"
        )


class _StubRuntime:
    def __init__(self, log, *, refuse=False, refuse_non_bytes=False):
        self.mode = "strict" if (refuse or refuse_non_bytes) else "off"
        self._log = log
        self._refuse = refuse
        self._refuse_non_bytes = refuse_non_bytes

    def reserve(self, selected, final_body):
        log = self._log
        refuse = self._refuse or (self._refuse_non_bytes and not isinstance(final_body, bytes))

        class _Ctx:
            async def __aenter__(self):
                log.append(("reserve", selected.topology, isinstance(final_body, bytes)))
                if refuse:
                    raise LocalProspectiveRefusal()
                return _StubPermit(log)

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


class _StubSession:
    def __init__(self, log, *, response=None, error=None):
        self._log = log
        self._response = response or SimpleNamespace(status=200, headers={})
        self._error = error

    async def request(self, method, url, **kwargs):
        self._log.append(("request", method, url))
        if self._error is not None:
            raise self._error
        return self._response


def _install_bridge(monkeypatch, log, *, refuse=False, refuse_non_bytes=False):
    runtime = _StubRuntime(log, refuse=refuse, refuse_non_bytes=refuse_non_bytes)
    selected_calls = []

    stub = SimpleNamespace(
        SelectedDispatch=lambda **kw: SimpleNamespace(**kw),
        LocalProspectiveRefusal=LocalProspectiveRefusal,
        get_runtime=lambda request: runtime,
        set_selected_dispatch=lambda request, value: selected_calls.append(value),
    )
    monkeypatch.setitem(sys.modules, "anthropic_throttle_proxy.prospective_runtime", stub)
    monkeypatch.setattr(ingress, "_prospective_bridge", lambda: stub)
    return selected_calls


async def _send(session, request, body=BODY):
    return await ingress._send_upstream(
        session,
        request,
        TARGET,
        body,
        aiohttp.ClientTimeout(total=5),
        None,
    )


async def test_off_mode_forwards_exact_final_bytes(monkeypatch):
    log = []
    _install_bridge(monkeypatch, log)
    session = _StubSession(log)
    response = await _send(session, make_mocked_request("POST", "/v1/messages"))
    assert response.status == 200
    assert ("request", "POST", TARGET) in log
    assert ("reserve", "ingress_relay", True) in log and "handoff" in log


async def test_strict_ingress_relay_refuses_before_transport(monkeypatch):
    log = []
    _install_bridge(monkeypatch, log, refuse=True)
    session = _StubSession(log)
    response = await _send(session, make_mocked_request("POST", "/v1/messages"))
    # Unsupported topology (ingress_relay) in strict: typed LOCAL refusal,
    # emitted before transport — no upstream call, no retry/spill/AIMD entry.
    assert response.status == 503
    assert ERROR_TYPE in response.text
    assert not any(item[0] == "request" for item in log)
    assert "handoff" not in log


async def test_observe_sends_after_reserve(monkeypatch):
    log = []
    _install_bridge(monkeypatch, log)
    session = _StubSession(log)
    response = await _send(session, make_mocked_request("POST", "/v1/messages"))
    assert response.status == 200
    assert log.index("handoff") < next(i for i, item in enumerate(log) if item[0] == "request")


async def test_selected_dispatch_metadata_per_attempt(monkeypatch):
    log = []
    selected_calls = _install_bridge(monkeypatch, log)
    session = _StubSession(log)
    await _send(session, make_mocked_request("POST", "/v1/messages"))
    await _send(session, make_mocked_request("POST", "/v1/messages"))
    # Per attempt, refreshed on every reroute: never a guessed identity.
    assert len(selected_calls) == 2
    for value in selected_calls:
        assert (
            value.credential_source is None
            and value.endpoint is None
            and value.topology == "ingress_relay"
            and value.internal_probe is False
        )


def test_inbound_provenance_claims_stripped_and_wellformed_unchanged():
    spoofed = make_mocked_request(
        "POST",
        "/v1/messages",
        headers={
            "content-type": "application/json",
            "x-throttle-prospective-refusal": "1",
            "x-throttle-refusal-source": "local",
            "x-throttle-prospective-budget": "forged",
        },
    )
    forwarded = ingress._forward_headers(spoofed, prospective_enabled=True)
    assert "x-throttle-prospective-refusal" not in forwarded
    assert "x-throttle-refusal-source" not in forwarded
    assert "x-throttle-prospective-budget" not in forwarded
    assert forwarded["content-type"] == "application/json"
    clean = make_mocked_request(
        "POST", "/v1/messages", headers={"content-type": "application/json"}
    )
    assert ingress._forward_headers(clean, prospective_enabled=True) == strip_incoming_provenance(
        dict(clean.headers)
    )


async def test_strict_streaming_body_is_typed_denial_before_transport(monkeypatch):
    # Unsupported accounting shape (non-bytes generation body) is an explicit
    # typed LOCAL refusal before transport — never silent forwarding.
    log = []
    _install_bridge(monkeypatch, log, refuse_non_bytes=True)
    session = _StubSession(log)

    async def _chunks():
        yield b"{}"

    response = await _send(session, make_mocked_request("POST", "/v1/messages"), body=_chunks())
    assert response.status == 503
    assert ERROR_TYPE in response.text
    assert not any(item[0] == "request" for item in log)
    assert ("reserve", "ingress_relay", False) in log


async def test_streaming_body_forwarded_with_reserve_in_off_observe(monkeypatch):
    # Positive controls (off and observe share the non-refusing path): the
    # streaming body OBJECT is handed to the bridge (classified UNKNOWN there)
    # and the request is forwarded unchanged.
    log = []
    _install_bridge(monkeypatch, log)
    session = _StubSession(log)

    async def _chunks():
        yield b"{}"

    response = await _send(session, make_mocked_request("POST", "/v1/messages"), body=_chunks())
    assert response.status == 200
    assert ("reserve", "ingress_relay", False) in log
    assert ("request", "POST", TARGET) in log


async def test_transport_error_is_not_a_local_refusal(monkeypatch):
    log = []
    _install_bridge(monkeypatch, log)
    session = _StubSession(log, error=aiohttp.ClientError("synthetic"))
    response = await _send(session, make_mocked_request("POST", "/v1/messages"))
    # Transport failures keep their existing shape: never local admission.
    assert response.status == 503
    assert ERROR_TYPE not in response.text
    assert b"ingress-upstream-unreachable" in response.body
