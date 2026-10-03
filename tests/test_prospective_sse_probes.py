"""Focused tests — T003/T004 slice: SSE seam, probes, local-refusal classification.

Offline only. Pins: LOCAL INCONCLUSIVE probe mapping, distinct attempt
provenance, keeper-before-single-terminal ordering, finalize/fast-fail feedback
skips for local refusals, and that a genuine provider 429 (or a raw spoofed
queue-timeout stamp) can NEVER suppress the AIMD feedback call.
"""

import asyncio

import pytest

from anthropic_throttle_proxy import proxy
from anthropic_throttle_proxy.prospective_refusal import (
    ProspectiveRefusal,
    RefusalReason,
    local_refusal_response,
)
from anthropic_throttle_proxy.prospective_runtime import (
    LocalProspectiveRefusal,
    SelectedDispatch,
)


def _refusal():
    return ProspectiveRefusal(
        reason=RefusalReason.EXHAUSTED, retry_after_s=30, budget_label="mimo-team-b"
    )


def _selected(internal_probe=True):
    return SelectedDispatch(
        credential_source="cred-a",
        endpoint="https://token-plan-sgp.xiaomimimo.com/v1/messages",
        topology="direct",
        internal_probe=internal_probe,
    )


class _RejectingProspective:
    """Runtime stub whose reserve entry refuses like strict-on-probe."""

    mode = "strict"

    def reserve(self, selected, final_body):
        raise LocalProspectiveRefusal(_refusal())


def test_probe_prospective_requires_internal_probe_flag():
    with pytest.raises(ValueError):
        proxy.ProbeProspective(_RejectingProspective(), _selected(internal_probe=False))


def test_probe_prospective_off_passthrough_handoff_is_noop():
    holder = proxy.ProbeProspective(None, _selected())
    ctx = holder.reserve(b"{}")
    assert isinstance(ctx, proxy._OffReservation)
    assert ctx.handoff() is None


def test_auth_probe_local_refusal_is_inconclusive_verdict_free(monkeypatch):
    # A local policy refusal is NOT a credential verdict: no note_upstream_auth
    # (never quarantine a valid credential), no fabricated probe budget.
    calls = []

    monkeypatch.setattr(proxy, "_api_key_candidate", lambda: {"token": "t"})
    monkeypatch.setattr(proxy.config, "AUTH_PROBE_MODEL", "probe-model", raising=False)
    monkeypatch.setattr(proxy, "note_upstream_auth", lambda *a, **k: calls.append(("verdict", a)))

    async def run():
        await proxy._probe_upstream_auth_once(
            prospective=proxy.ProbeProspective(_RejectingProspective(), _selected())
        )

    asyncio.run(run())
    assert calls == []


def test_credential_recheck_local_refusal_keeps_quarantine(monkeypatch):
    # LOCAL INCONCLUSIVE: cadence touch only; quarantine untouched; the
    # recovery path (_clear_bearer_credential) is never reached.
    events = []
    monkeypatch.setattr(proxy, "_meter_binding_allows", lambda bid: True)
    monkeypatch.setattr(proxy, "_touch_credential_check", lambda bid: events.append(("touch", bid)))
    monkeypatch.setattr(proxy, "_clear_bearer_credential", lambda *a: events.append(("clear", a)))

    async def run():
        await proxy._credential_recheck_one(
            "bid-x",
            "tok",
            prospective=proxy.ProbeProspective(_RejectingProspective(), _selected()),
        )

    asyncio.run(run())
    assert events == [("touch", "bid-x")]


def test_attempt_provenance_flags_are_distinct():
    attempt = proxy._Attempt()
    assert attempt.local_meter_refused is False
    assert attempt.local_prospective_refused is False
    assert attempt.aimd_owned is False


def test_finalize_aimd_called_for_provider_429_but_never_for_local_refusal(monkeypatch):
    recorded = []

    async def fake_aimd(bid, limiter, attempt):
        recorded.append(bid)

    monkeypatch.setattr(proxy, "_aimd_feedback", fake_aimd)

    # Genuine provider outcome: no local flags -> AIMD feedback IS applied.
    attempt = proxy._Attempt()
    asyncio.run(proxy._finalize_aimd_feedback("bid", object(), attempt, False))
    assert recorded == ["bid"]

    # Local prospective refusal: explicit internal provenance -> never applied.
    recorded.clear()
    refused = proxy._Attempt()
    refused.local_prospective_refused = True
    refused.aimd_owned = True
    asyncio.run(proxy._finalize_aimd_feedback("bid", object(), refused, False))
    assert recorded == []

    # Plan-meter provenance keeps its own label and its own skip.
    recorded.clear()
    meter = proxy._Attempt()
    meter.local_meter_refused = True
    meter.aimd_owned = True
    asyncio.run(proxy._finalize_aimd_feedback("bid", object(), meter, False))
    assert recorded == []


def test_spoofed_queue_timeout_stamp_cannot_suppress_aimd_call(monkeypatch):
    # A RAW provider response carrying a forged queue-timeout stamp must not
    # suppress feedback at the finalize seam; marker validation lives in the
    # merged anti-spoof suites and cannot be bypassed by meta alone.
    recorded = []

    async def fake_aimd(bid, limiter, attempt):
        recorded.append(attempt.meta)

    monkeypatch.setattr(proxy, "_aimd_feedback", fake_aimd)
    attempt = proxy._Attempt()
    attempt.final_status = 429
    attempt.meta = {"x-anthropic-throttle-queue-timeout": "1"}
    asyncio.run(proxy._finalize_aimd_feedback("bid", object(), attempt, False))
    assert recorded == [attempt.meta]


def test_fast_fail_skips_local_prospective_refusal():
    attempt = proxy._Attempt()
    attempt.local_prospective_refused = True
    assert proxy._maybe_fast_fail_throttle_direct("bid", "p", object(), attempt) is None


def test_local_refusal_precommit_typed_503_contract():
    response = local_refusal_response(_refusal())
    assert response.status == 503
    assert response.headers.get("X-Throttle-Prospective-Refusal") == "1"
    assert response.headers.get("Retry-After") == "30"


class _RecorderSse:
    def __init__(self):
        self.writes = []
        self.eof = False

    async def write(self, data):
        self.writes.append(data)

    async def write_eof(self):
        self.eof = True


def test_keeper_stops_before_exactly_one_terminal(monkeypatch):
    order = []
    sse = _RecorderSse()
    attempt = proxy._Attempt()

    async def cancel_keepalive():
        order.append("keeper-off")

    async def refuse(*args, **kwargs):
        raise LocalProspectiveRefusal(_refusal())

    actual_emit = proxy._emit_sse_error_terminal

    async def emit(*args, **kwargs):
        order.append("terminal")
        await actual_emit(*args, **kwargs)

    monkeypatch.setattr(proxy, "_meter_binding_allows", lambda bid: True)
    monkeypatch.setattr(proxy, "_forward_once_into_sse", refuse)
    monkeypatch.setattr(proxy, "_emit_sse_error_terminal", emit)

    async def run():
        return await proxy._keepalive_one_attempt(
            request=object(),
            headers={},
            body=b"{}",
            url="https://synthetic.invalid",
            client_timeout=object(),
            via="direct",
            wait_deadline=None,
            sse_resp=sse,
            attempt=attempt,
            bid="synthetic",
            path="/v1/messages",
            limiter=object(),
            cancel_keepalive=cancel_keepalive,
        )

    assert asyncio.run(run()) is sse
    assert order == ["keeper-off", "terminal"]
    assert sse.eof
    assert b"".join(sse.writes).count(b"event: error") == 1
    assert b"prospective_admission_refused" in b"".join(sse.writes)
    assert attempt.local_prospective_refused and attempt.aimd_owned
    assert attempt.final_status == 503
    assert attempt.meta is None and attempt.captured is None


async def test_prepared_sse_strict_refuses_without_transport(tmp_path):
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer, make_mocked_request
    from test_prospective_forwarding_live import _BODY, _runtime

    from anthropic_throttle_proxy.prospective_runtime import RUNTIME_KEY

    hits = []

    async def upstream(request):
        hits.append(await request.read())
        return web.Response(text="unexpected transport")

    app = web.Application()
    app.router.add_post("/v1/messages", upstream)
    async with TestServer(app) as server:
        endpoint = str(server.make_url("")).rstrip("/")
        runtime = _runtime(tmp_path, endpoint)
        await runtime.start()
        request_app = web.Application()
        request_app[RUNTIME_KEY] = runtime
        request = make_mocked_request("POST", "/v1/messages", app=request_app)
        try:
            with pytest.raises(LocalProspectiveRefusal):
                await proxy._forward_once_into_sse(
                    request,
                    {},
                    _BODY,
                    endpoint + "/v1/messages",
                    aiohttp.ClientTimeout(total=2),
                    _RecorderSse(),
                )
            assert hits == []
        finally:
            await runtime.aclose()
