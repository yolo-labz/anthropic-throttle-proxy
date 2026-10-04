"""Synthetic numeric calibration; no provider traffic or operator state."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from multidict import CIMultiDict
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from anthropic_throttle_proxy import forwarding, proxy
from anthropic_throttle_proxy import prospective_calibration as calibration
from anthropic_throttle_proxy.ledger import Budgets
from anthropic_throttle_proxy.prospective import TokenAccounting, account_request
from anthropic_throttle_proxy.prospective_admission import Scope
from anthropic_throttle_proxy.prospective_runtime import (
    RUNTIME_KEY,
    ProspectiveRuntime,
    SelectedDispatch,
    set_selected_dispatch,
)
from anthropic_throttle_proxy.prospective_scope import ScopeResolver

PATH = "/v1/chat/completions"
HEADERS = CIMultiDict({"Content-Type": "text/event-stream"})
COST = TokenAccounting(20, 60, "private-model")
BODY = (
    b'{"model":"private-model","messages":[{"role":"user","content":"private-prompt"}],'
    b'"max_tokens":60}'
)
KEY = ("private-upstream", "private-account", "private-model")
ENDPOINT = "https://fixture.invalid"
SELECTED = SelectedDispatch("private-source", ENDPOINT, "direct", False)


def frame(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def chat(usage=None, finish="stop"):
    return frame({"choices": [{"index": 0, "finish_reason": finish}], "usage": usage})


VALID = chat({"prompt_tokens": 30, "completion_tokens": 5}) + b"data: [DONE]\n\n"


@pytest.fixture
def registry(monkeypatch):
    registry = CollectorRegistry()
    for attr, metric in (
        (
            "M_CALIBRATION_SAMPLES",
            Counter("samples", "test", ["scope", "outcome", "terminal"], registry=registry),
        ),
        ("M_CALIBRATION_TOKENS", Counter("tokens", "test", ["scope", "kind"], registry=registry)),
        ("M_CALIBRATION_INPUT_RATIO", Histogram("ratio", "test", ["scope"], registry=registry)),
    ):
        monkeypatch.setattr(calibration, attr, metric)
    return registry


def sample(registry, outcome, terminal):
    return (
        registry.get_sample_value(
            "samples_total", {"scope": "fixture-public", "outcome": outcome, "terminal": terminal}
        )
        or 0
    )


def paired(registry, kind):
    return registry.get_sample_value("tokens_total", {"scope": "fixture-public", "kind": kind})


def complete(body=VALID, *, headers=HEADERS, path=PATH, status=200, single_scope=True):
    attempt = calibration.CalibrationAttempt("fixture-public", COST, single_scope)
    attempt.sent = True
    with attempt:
        attempt.response(body, status, headers, path)
    return attempt


@pytest.mark.parametrize(
    "usage,outcome",
    [
        ({"prompt_tokens": 0, "completion_tokens": 0}, "comparable"),
        ({"prompt_tokens": 1}, "missing_usage"),
        (None, "missing_usage"),
        ({"prompt_tokens": True, "completion_tokens": 1}, "malformed_usage"),
        ({"prompt_tokens": "10", "completion_tokens": 1}, "malformed_usage"),
        ({"prompt_tokens": 1.5, "completion_tokens": 1}, "malformed_usage"),
        ({"prompt_tokens": -1, "completion_tokens": 1}, "malformed_usage"),
        ({"prompt_tokens": 2**60, "completion_tokens": 1}, "malformed_usage"),
        ({"prompt_tokens": float("nan"), "completion_tokens": 1}, "malformed_usage"),
        ({"input_tokens": 1, "prompt_tokens": 1, "completion_tokens": 1}, "malformed_usage"),
    ],
)
def test_absence_zero_and_malformed_usage(registry, usage, outcome):
    complete(chat(usage))
    assert sample(registry, outcome, "eof_with_finish") == 1
    assert (paired(registry, "reported_input") is not None) == (outcome == "comparable")


def test_chat_cumulative_usage_and_cached_reasoning_subsets(registry):
    first = {"prompt_tokens": 30, "completion_tokens": 2}
    final = first | {
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    complete(chat(first, None) + chat(final) + chat(final))
    assert sample(registry, "comparable", "eof_with_finish") == 1
    assert paired(registry, "reported_input") == 30  # not 90 or 30+20
    assert paired(registry, "reported_output") == 5  # not 2+5+5 or 5+3
    assert paired(registry, "estimated_input") == 20
    assert paired(registry, "output_bound") == 60
    assert registry.get_sample_value("ratio_sum", {"scope": "fixture-public"}) == 1.5


@pytest.mark.parametrize(
    "body",
    [
        chat({"prompt_tokens": 20, "completion_tokens": 4})
        + chat({"prompt_tokens": 20, "completion_tokens": 3}),
        chat(
            {
                "prompt_tokens": 20,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 21},
            }
        ),
        b'data: {"usage":{"prompt_tokens":1,"prompt_tokens":2,"completion_tokens":1}}\n\n',
        b"data: {\n\n",
    ],
)
def test_ambiguous_invalid_frames_never_pair(registry, body):
    complete(body)
    assert paired(registry, "reported_input") is None


def test_anthropic_explicit_full_input_and_cumulative_output(registry):
    start = frame(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 0,
                }
            },
        }
    )
    delta = frame(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 7},
        }
    )
    complete(start + delta + delta + frame({"type": "message_stop"}), path="/v1/messages")
    assert sample(registry, "comparable", "eof_with_finish") == 1
    assert paired(registry, "reported_input") == 35
    assert paired(registry, "reported_output") == 7


@pytest.mark.parametrize(
    "body,outcome,terminal",
    [
        (b"", "missing_usage", "eof_without_finish"),
        (b"data: [DONE]\n\n", "missing_usage", "eof_without_finish"),
        (
            chat({"prompt_tokens": 10, "completion_tokens": 1}, None),
            "missing_finish",
            "eof_without_finish",
        ),
        (chat(None), "missing_usage", "eof_with_finish"),
        (VALID[:-1], "malformed_usage", "eof_unverified"),
        (b"x" * calibration.CAPTURE_LIMIT, "capture_cap", "eof_unverified"),
        (None, "missing_capture", "eof_unverified"),
    ],
)
def test_terminal_outcome_is_independent_from_usage(registry, body, outcome, terminal):
    complete(body)
    assert sample(registry, outcome, terminal) == 1
    assert paired(registry, "estimated_input") is None


def test_encoded_and_multi_scope_samples_do_not_claim_accuracy(registry):
    complete(headers=CIMultiDict({"Content-Type": "text/event-stream", "Content-Encoding": "gzip"}))
    complete(single_scope=False)
    assert sample(registry, "encoded", "eof_unverified") == 1
    assert sample(registry, "ambiguous_scope", "eof_with_finish") == 1
    assert paired(registry, "reported_input") is None


@pytest.mark.parametrize(
    "exc", [asyncio.CancelledError(), TimeoutError(), TypeError("private-error")]
)
def test_transport_exception_identity_and_exactly_once(registry, exc):
    attempt = calibration.CalibrationAttempt("fixture-public", COST, True)
    attempt.sent = True
    with pytest.raises(type(exc)) as caught, attempt:
        attempt.response(VALID, 200, HEADERS, PATH)
        raise exc  # even a late transport failure invalidates the provisional sample
    assert caught.value is exc
    outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "transport_error"
    attempt.__exit__(None, None, None)
    assert sample(registry, outcome, outcome) == 1
    assert paired(registry, "reported_input") is None
    assert b"private-error" not in generate_latest(registry)


def test_no_handoff_means_no_sample_and_off_never_parses(registry, monkeypatch):
    with calibration.CalibrationAttempt("fixture-public", COST, True) as attempt:
        attempt.response(VALID, 200, HEADERS, PATH)
    assert list(registry.collect())[0].samples == []

    def forbidden(*args):
        pytest.fail("off path parsed response")

    monkeypatch.setattr(calibration, "_parse", forbidden)
    with calibration.calibration_attempt(None) as disabled:
        disabled.response(object(), object(), object(), object())
    assert paired(registry, "reported_input") is None


class Request(dict):
    method = "POST"
    path = PATH

    def __init__(self, runtime):
        super().__init__()
        self.app = {RUNTIME_KEY: runtime}
        set_selected_dispatch(self, SELECTED)


def make_runtime(tmp_path, endpoint=ENDPOINT, mode="observe"):
    budgets = Budgets(1, 1000)
    resolver = ScopeResolver(
        [
            {
                "source": SELECTED.credential_source,
                "endpoint": endpoint,
                "alias": KEY[2],
                "upstream": KEY[0],
                "account": KEY[1],
                "model": KEY[2],
                "budgets": budgets,
            }
        ],
        sources=[SELECTED.credential_source],
        endpoints=[endpoint],
        models=[KEY[2]],
    )
    return ProspectiveRuntime(
        mode=mode,
        resolver=resolver,
        scopes=[Scope(KEY, budgets, str(tmp_path / "ledger.json"), True)],
        max_pending=2,
        wait_timeout=2,
        budget_label="fixture-public",
        retry_after_s=1,
    )


@pytest.fixture
async def runtime(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.aclose()


class Transport:
    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=self.status, headers=HEADERS)

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, transports, wires):
        self.transports, self.wires = transports, wires

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.wires.append(kwargs["data"])
        return self.transports.pop(0)


@pytest.mark.parametrize("sse", [False, True])
async def test_real_observe_permit_pairs_each_attempt_without_scope_leaks(
    registry, runtime, monkeypatch, sse
):
    from anthropic_throttle_proxy import pacing

    wires = []
    transports = [Transport(), Transport(error=TimeoutError()), Transport()]
    monkeypatch.setattr(aiohttp, "TCPConnector", lambda **kw: object())
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: Session(transports, wires))

    async def pace():
        pass

    async def stream(*args):
        return None, 200, bytearray(VALID), None, {}

    async def pipe(*args):
        return bytearray(VALID)

    monkeypatch.setattr(forwarding, "_pace_dispatch", pace)
    monkeypatch.setattr(pacing, "_pace_dispatch", pace)
    monkeypatch.setattr(forwarding, "_stream_response", stream)
    monkeypatch.setattr(proxy, "_pipe_sse_upstream", pipe)
    request = Request(runtime)
    for _ in range(3):
        args = (request, {}, BODY, ENDPOINT + PATH, aiohttp.ClientTimeout(total=1))
        if sse:
            await proxy._forward_once_into_sse(*args, sse_resp=object())
        else:
            await forwarding._forward_once(*args)
        await asyncio.gather(*tuple(runtime._observing))
    assert wires == [BODY] * 3
    assert sample(registry, "comparable", "eof_with_finish") == 2
    assert sample(registry, "transport_error", "transport_error") == 1
    assert paired(registry, "estimated_input") == 2 * account_request(BODY, {}).input_tokens
    assert runtime.observations()["exhausted"] == 2  # pairing not biased by shadow denial
    exported = generate_latest(registry)
    for private in (
        b"private-model",
        b"private-account",
        b"private-source",
        b"private-prompt",
        ENDPOINT.encode(),
    ):
        assert private not in exported


@pytest.mark.parametrize(
    "extra",
    [
        {"total_tokens": 36},
        {"total_tokens": True},
        {"prompt_cache_hit_tokens": 5},
        {"prompt_cache_miss_tokens": 25},
        {"prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 30},
        {"prompt_cache_hit_tokens": -1, "prompt_cache_miss_tokens": 31},
        {
            "prompt_cache_hit_tokens": 5,
            "prompt_cache_miss_tokens": 25,
            "prompt_tokens_details": {"cached_tokens": 6},
        },
        {"prompt_tokens_details": []},
        {"completion_tokens_details": {"reasoning_tokens": 6}},
    ],
)
def test_conflicting_chat_totals_never_pair(registry, extra):
    complete(chat({"prompt_tokens": 30, "completion_tokens": 5} | extra))
    assert sample(registry, "malformed_usage", "eof_with_finish") == 1
    assert paired(registry, "reported_input") is None


def test_json_response_and_cache_partition_preserve_full_input(registry):
    usage = {
        "prompt_tokens": 30,
        "completion_tokens": 5,
        "total_tokens": 35,
        "prompt_cache_hit_tokens": 5,
        "prompt_cache_miss_tokens": 25,
        "prompt_tokens_details": {"cached_tokens": 5},
    }
    body = json.dumps({"choices": [{"finish_reason": "stop"}], "usage": usage}).encode()
    complete(body, headers=CIMultiDict({"Content-Type": "application/json; charset=utf-8"}))
    assert paired(registry, "reported_input") == 30
    assert sample(registry, "comparable", "eof_with_finish") == 1


@pytest.mark.parametrize("body", [frame({"error": {}}), frame({"type": "error"})])
def test_sse_error_cannot_become_a_pair_after_valid_usage(registry, body):
    complete(VALID + body)
    assert sample(registry, "malformed_usage", "eof_with_finish") == 1
    assert paired(registry, "reported_input") is None


@pytest.mark.parametrize("status", [429, 503])
def test_upstream_refusal_never_pairs_even_with_complete_counts(registry, status):
    complete(status=status)
    assert sample(registry, "upstream_error", "eof_with_finish") == 1
    assert paired(registry, "reported_input") is None


def test_unsupported_and_malformed_envelopes_are_unverified(registry):
    complete(path="/v1/responses")
    assert sample(registry, "unsupported_protocol", "eof_unverified") == 1
    complete(headers=CIMultiDict({"Content-Type": "text/plain"}))
    complete(b"[]", headers=CIMultiDict({"Content-Type": "application/json"}))
    complete(b"data: \xff\n\n")
    assert sample(registry, "malformed_usage", "eof_unverified") == 3


def test_metric_failure_never_changes_transport_result(registry, monkeypatch):
    def fail(_self):
        raise RuntimeError("metric unavailable")

    monkeypatch.setattr(calibration.CalibrationAttempt, "_record", fail)
    complete()
    exc = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        attempt = calibration.CalibrationAttempt("fixture-public", COST, True)
        attempt.sent = True
        with attempt:
            raise exc
    assert caught.value is exc


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("mode", ["off", "observe", "strict"])
async def test_real_loopback_wire_and_metrics_surface(registry, tmp_path, sse, mode):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    wires = []

    async def origin(request):
        wires.append(await request.read())
        return web.Response(body=VALID, headers=HEADERS)

    origin_app = web.Application()
    origin_app.router.add_post(PATH, origin)
    async with TestServer(origin_app) as upstream:
        endpoint = str(upstream.make_url("/")).rstrip("/")
        runtime = make_runtime(tmp_path, endpoint, mode)
        await runtime.start()

        async def relay(request):
            set_selected_dispatch(
                request, SelectedDispatch(SELECTED.credential_source, endpoint, "direct", False)
            )
            args = (
                request,
                {},
                await request.read(),
                endpoint + PATH,
                aiohttp.ClientTimeout(total=2),
            )
            if sse:
                response = web.StreamResponse(headers=HEADERS)
                await response.prepare(request)
                status, _, captured, error = await proxy._forward_once_into_sse(
                    *args, sse_resp=response
                )
                assert status == 200 and captured == VALID and error is None
                await response.write_eof()
                return response
            response, status, captured, error, _ = await forwarding._forward_once(*args)
            assert status == 200 and captured == VALID and error is None
            return response

        async def metrics(_request):
            return web.Response(body=generate_latest(registry), content_type="text/plain")

        app = web.Application()
        app[RUNTIME_KEY] = runtime
        app.router.add_post(PATH, relay)
        app.router.add_get("/metrics", metrics)
        try:
            async with TestClient(TestServer(app)) as client:
                response = await client.post(PATH, data=BODY)
                assert response.status == 200
                assert await response.read() == VALID
                if mode == "observe":
                    await asyncio.gather(*tuple(runtime._observing))
                response = await client.get("/metrics")
                surface = await response.text()
                assert response.status == 200
                assert ('outcome="comparable"' in surface) == (mode == "observe")
                assert wires == [BODY]
                for private in ("private-model", "private-prompt", "private-account", endpoint):
                    assert private not in surface
        finally:
            await runtime.aclose()
