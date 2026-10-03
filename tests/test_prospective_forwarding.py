"""T003 prospective-forwarding reservation seam tests (synthetic, no network).

Fake doubles stand in for the #297 bridge (``prospective_runtime``) and for the
aiohttp transport; nothing here implements owner/ledger/runtime semantics — the
doubles only record the seam's contract:

* reserve on the FINAL body, after normalize/fit/rebind and AFTER pacing,
* synchronous ``permit.handoff()`` immediately before ``session.request`` with
  no intervening await,
* one reserve per actual attempt (no double debit across caller retries),
* typed ``LocalProspectiveRefusal`` propagates untouched (zero upstream
  requests), transport exceptions are never turned into local policy,
* unsent (handoff never completed) is a rollback exit; handoff-completed is a
  retain exit,
* default-off / control-GET paths never enter the generation API and keep wire
  bytes identical.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from anthropic_throttle_proxy import forwarding


class LocalProspectiveRefusal(Exception):
    """Typed stand-in matching the #297 exception contract (``.refusal``)."""

    def __init__(self, refusal="stub-refusal"):
        super().__init__("local prospective admission refused")
        self.refusal = refusal


class FakePermit:
    def __init__(self, events, *, fail_handoff=None):
        self._events = events
        self._fail_handoff = fail_handoff
        self.handoff_done = False
        self.exit = None

    def handoff(self):
        self._events.append("handoff")
        if self._fail_handoff is not None:
            raise self._fail_handoff
        self.handoff_done = True


class FakeReservation:
    def __init__(self, events, permit, *, fail_enter=None):
        self._events = events
        self._permit = permit
        self._fail_enter = fail_enter

    async def __aenter__(self):
        self._events.append("reserve-enter")
        if self._fail_enter is not None:
            raise self._fail_enter
        return self._permit

    async def __aexit__(self, *exc):
        self._permit.exit = "retained" if self._permit.handoff_done else "rolled-back"
        self._events.append(self._permit.exit)
        return False


class FakeRuntime:
    mode = "strict"

    def __init__(self, events, *, fail_enter=None, fail_handoff=None):
        self._events = events
        self._fail_enter = fail_enter
        self._fail_handoff = fail_handoff
        self.reserves = []

    def reserve(self, selected, final_body):
        self._events.append("reserve-construct")
        self.reserves.append((selected, final_body))
        permit = FakePermit(self._events, fail_handoff=self._fail_handoff)
        return FakeReservation(self._events, permit, fail_enter=self._fail_enter)


class FakeBridge:
    """Test double for the #297 accessor pair — not a runtime implementation."""

    def __init__(self, events, runtime, selected=None):
        self._events = events
        self._runtime = runtime
        self._selected = selected
        self.get_runtime_calls = 0

    def get_runtime(self, request):
        self.get_runtime_calls += 1
        return self._runtime

    def get_selected_dispatch(self, request):
        return self._selected


class FakeTransport:
    def __init__(self, events, calls, *, raise_on_enter=None):
        self._events = events
        self._calls = calls
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        self._events.append("request")
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return SimpleNamespace(status=200, headers={})

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, events, calls, *, raise_on_enter=None):
        self._events = events
        self._calls = calls
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def request(self, method, url, headers, data, allow_redirects):
        self._calls.append({"method": method, "url": url, "headers": dict(headers), "data": data})
        return FakeTransport(self._events, self._calls, raise_on_enter=self._raise_on_enter)


def _request(method="POST"):
    class Request(dict):
        pass

    request = Request()
    request.method = method
    request.path = "/v1/messages"
    request.app = {}
    return request


_BODY = b'{"model":"m","messages":[]}'
_TIMEOUT = aiohttp.ClientTimeout(total=1)
_CANNED = (None, 200, bytearray(b"ok"), None, {})


def _wire(monkeypatch, events, calls, *, raise_on_enter=None):
    session = FakeSession(events, calls, raise_on_enter=raise_on_enter)
    monkeypatch.setattr(forwarding.aiohttp, "TCPConnector", lambda **kw: object())
    monkeypatch.setattr(forwarding.aiohttp, "ClientSession", lambda **kw: session)

    async def pace():
        events.append("pace")

    async def stream(request, upstream):
        return _CANNED

    monkeypatch.setattr(forwarding, "_pace_dispatch", pace)
    monkeypatch.setattr(forwarding, "_stream_response", stream)


def _run(request=None, body=_BODY, headers=None):
    return asyncio.run(
        forwarding._forward_once(
            request if request is not None else _request(),
            headers if headers is not None else {"X-Test": "1"},
            body,
            "https://upstream.example/v1/chat/completions",
            _TIMEOUT,
        )
    )


# ------------------------------------------------------------------ denial


def test_strict_denial_never_reaches_upstream(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)
    runtime = FakeRuntime(events, fail_enter=LocalProspectiveRefusal())
    monkeypatch.setattr(forwarding, "prospective_runtime", FakeBridge(events, runtime))

    with pytest.raises(LocalProspectiveRefusal) as caught:
        _run()

    assert caught.value.refusal == "stub-refusal"  # typed payload stays reachable
    assert calls == []
    assert "request" not in events


def test_handoff_failure_is_typed_unsent_and_never_reaches_upstream(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)
    runtime = FakeRuntime(events, fail_handoff=LocalProspectiveRefusal())
    monkeypatch.setattr(forwarding, "prospective_runtime", FakeBridge(events, runtime))

    with pytest.raises(LocalProspectiveRefusal):
        _run()

    assert calls == []
    assert events[-1] == "rolled-back"  # handoff never completed => unsent rollback


# ------------------------------------------------------------- happy paths


def test_reserve_after_pace_handoff_immediately_before_transport(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)
    runtime = FakeRuntime(events)
    monkeypatch.setattr(forwarding, "prospective_runtime", FakeBridge(events, runtime))

    result = _run()

    assert result == _CANNED
    assert events == [
        "pace",
        "reserve-construct",
        "reserve-enter",
        "handoff",
        "request",
        "retained",
    ]
    assert events.index("handoff") == events.index("request") - 1


def test_reserve_receives_selected_and_final_body(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)
    runtime = FakeRuntime(events)
    selected = object()
    monkeypatch.setattr(
        forwarding, "prospective_runtime", FakeBridge(events, runtime, selected=selected)
    )
    monkeypatch.setattr(
        forwarding, "normalize_text_content_blocks", lambda body, url: b"NORMALIZED"
    )
    monkeypatch.setattr(
        forwarding,
        "fit_chat_completions_body",
        lambda body, url: (b"FINAL", {"fitted": True, "turns_dropped": 1}),
    )

    _run()

    assert runtime.reserves == [(selected, b"FINAL")]
    assert calls[0]["data"] == b"FINAL"


def test_one_reserve_per_attempt_across_caller_retries(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)
    runtime = FakeRuntime(events)
    monkeypatch.setattr(forwarding, "prospective_runtime", FakeBridge(events, runtime))

    _run()
    _run()  # the caller's retry is a fresh attempt

    assert len(runtime.reserves) == 2
    assert events.count("handoff") == 2
    assert len(calls) == 2


def test_sent_outcome_retains_debt(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls, raise_on_enter=TimeoutError("boom"))
    runtime = FakeRuntime(events)
    monkeypatch.setattr(forwarding, "prospective_runtime", FakeBridge(events, runtime))

    result = _run()

    # Existing consumer contract: transport failure is returned unchanged, not
    # translated into local admission policy.
    assert result[0] is None and isinstance(result[3], TimeoutError)
    assert events[-1] == "retained"  # handoff completed => sent/unknown keeps debt


def test_client_disconnect_propagates_unchanged(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)
    runtime = FakeRuntime(events)
    monkeypatch.setattr(forwarding, "prospective_runtime", FakeBridge(events, runtime))

    async def boom(request, upstream):
        raise aiohttp.ClientConnectionResetError()

    monkeypatch.setattr(forwarding, "_stream_response", boom)

    with pytest.raises(aiohttp.ClientConnectionResetError):
        _run()

    assert events[-1] == "retained"


# ------------------------------------------------------- default-off parity


def test_default_off_real_bridge_keeps_wire_bytes(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)

    result = _run(headers={"X-Test": "1", "Content-Length": str(len(_BODY))})

    assert result == _CANNED
    assert events == ["pace", "request"]
    assert calls == [
        {
            "method": "POST",
            "url": "https://upstream.example/v1/chat/completions",
            "headers": {"X-Test": "1", "Content-Length": str(len(_BODY))},
            "data": _BODY,
        }
    ]


def test_control_get_never_enters_generation_api(monkeypatch):
    events, calls = [], []
    _wire(monkeypatch, events, calls)
    runtime = FakeRuntime(events)
    bridge = FakeBridge(events, runtime)
    monkeypatch.setattr(forwarding, "prospective_runtime", bridge)

    result = _run(request=_request(method="GET"), body=None)

    assert result == _CANNED
    assert bridge.get_runtime_calls == 0
    assert runtime.reserves == []
    assert events == ["pace", "request"]
