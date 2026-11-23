"""Attempt correlation on usage-ledger rows (RLC-06): claims, provider ids, retries, overlap.

Synthetic transport only: a stub upstream that this file controls, behind the real proxy app.
A client header is a CLAIM; these tests pin what the gateway records and what it refuses to infer.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict
from test_keepalive_hold import _make_proxy_app
from test_proxy_app import _SSE_BODY, _reset_proxy_state

from anthropic_throttle_proxy import config, limiter, pacing
from anthropic_throttle_proxy import usage_ledger as ul

HEADER = "x-pi-attempt-id"
PROVIDER_A = "req_011CSaaaaaaaaaaaaaaaa"
PROVIDER_B = "req_011CSbbbbbbbbbbbbbbbb"


def _uid() -> str:
    return str(uuid.uuid4())


class _Req(dict):
    """The two things the ledger reads from a request: ``headers`` and item storage."""

    def __init__(self, *pairs: tuple[str, str]) -> None:
        super().__init__()
        self.headers = CIMultiDict(pairs)


@pytest.fixture(autouse=True)
def _fresh_window():
    ul._seen_ids.clear()
    yield
    ul._seen_ids.clear()


@pytest.fixture
def capture_on(monkeypatch):
    monkeypatch.setattr(config, "USAGE_LEDGER_MODE", "observe")
    monkeypatch.setattr(config, "ATTEMPT_ID_CAPTURE_MODE", "observe")


# ---------------------------------------------------------------- claim rules (unit)


def test_off_by_default_and_needs_the_ledger(monkeypatch):
    req = _Req((HEADER, _uid()))
    monkeypatch.setattr(config, "USAGE_LEDGER_MODE", "off")
    monkeypatch.setattr(config, "ATTEMPT_ID_CAPTURE_MODE", "observe")
    assert ul.correlation_of(req) == {}
    monkeypatch.setattr(config, "USAGE_LEDGER_MODE", "observe")
    monkeypatch.setattr(config, "ATTEMPT_ID_CAPTURE_MODE", "off")
    assert ul.correlation_of(req) == {}
    monkeypatch.setattr(config, "ATTEMPT_ID_CAPTURE_MODE", "1")  # not exactly "observe"
    assert ul.correlation_of(req) == {}


def test_one_valid_first_seen_value_is_claimed(capture_on):
    value = _uid()
    got = ul.correlation_of(_Req((HEADER, value)))
    assert got["attempt_binding"] == "claimed"
    assert got["x_pi_attempt_id"] == value
    assert got["physical_attempts"] == 0  # nothing was sent: the gateway counts, it does not guess
    assert got["provider_request_id"] is None
    assert len(got["gateway_request_id"]) == 32


def test_missing_header_is_missing_not_inferred(capture_on):
    got = ul.correlation_of(_Req(("authorization", "Bearer x")))
    assert got["attempt_binding"] == "missing"
    assert got["x_pi_attempt_id"] is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        str(uuid.uuid4()).upper(),  # canonical lower-case only
        str(uuid.uuid1()),  # a producer id is random (v4)
        str(uuid.uuid4()) + " ",
        str(uuid.uuid4()) + "\n",
        "x" * 5000,
    ],
)
def test_invalid_values_are_rejected_and_never_stored(capture_on, value):
    got = ul.correlation_of(_Req((HEADER, value)))
    assert got["attempt_binding"] == "invalid"
    assert got["x_pi_attempt_id"] is None
    assert value.strip() == "" or value not in json.dumps(got)


def test_repeated_and_conflicting_headers_are_refused(capture_on):
    same = _uid()
    got = ul.correlation_of(_Req((HEADER, same), (HEADER, same)))
    assert (got["attempt_binding"], got["x_pi_attempt_id"]) == ("repeated", None)
    got = ul.correlation_of(_Req((HEADER, _uid()), (HEADER, _uid())))
    assert (got["attempt_binding"], got["x_pi_attempt_id"]) == ("conflicting", None)


def test_a_reused_id_on_a_second_request_is_replayed(capture_on):
    value = _uid()
    first = ul.correlation_of(_Req((HEADER, value)))
    second = ul.correlation_of(_Req((HEADER, value)))
    assert first["attempt_binding"] == "claimed"
    assert (second["attempt_binding"], second["x_pi_attempt_id"]) == ("replayed", None)
    assert first["gateway_request_id"] != second["gateway_request_id"]


def test_seen_window_is_bounded(capture_on, monkeypatch):
    monkeypatch.setattr(ul, "_SEEN_MAX", 3)
    ids = [_uid() for _ in range(5)]
    for value in ids:
        ul.correlation_of(_Req((HEADER, value)))
    assert len(ul._seen_ids) == 3
    # the oldest fell out of the window: a documented limit, not a silent join
    assert ul.correlation_of(_Req((HEADER, ids[0])))["attempt_binding"] == "claimed"


class _Up:
    def __init__(self, **headers: str) -> None:
        self.headers = CIMultiDict({k.replace("_", "-"): v for k, v in headers.items()})


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"request_id": PROVIDER_A}, PROVIDER_A),
        ({"x_request_id": PROVIDER_A}, PROVIDER_A),
        ({"request_id": PROVIDER_A, "x_request_id": PROVIDER_A}, PROVIDER_A),
        ({"request_id": PROVIDER_A, "x_request_id": PROVIDER_B}, None),  # disagree: neither
        ({"request_id": "req_short"}, None),
        ({"request_id": "<script>alert(1)</script>"}, None),
        ({"request_id": "req_" + "a" * 200}, None),
        ({"request_id": PROVIDER_A + "\n"}, None),
        ({}, None),
    ],
)
def test_provider_request_id_shape(capture_on, headers, expected):
    req = _Req((HEADER, _uid()))
    ul.note_physical_attempt(req)
    ul.note_response(req, _Up(**headers))
    got = ul.correlation_of(req, "direct")
    assert got["provider_request_ids"] == [expected]
    assert got["provider_request_id"] == expected


def test_retry_keeps_every_response_but_names_no_single_provider_id(capture_on):
    req = _Req((HEADER, _uid()))
    for provider in (PROVIDER_A, PROVIDER_B):
        ul.note_physical_attempt(req)
        ul.note_response(req, _Up(request_id=provider))
    got = ul.correlation_of(req, "direct")
    assert got["physical_attempts"] == 2 and got["upstream_responses"] == 2
    assert got["retry_observed"] is True
    assert got["provider_request_ids"] == [PROVIDER_A, PROVIDER_B]
    assert got["provider_request_id"] is None  # which one belongs to the client claim? unknown


def test_send_without_response_is_visible(capture_on):
    req = _Req((HEADER, _uid()))
    ul.note_physical_attempt(req)  # connect failed, nothing answered
    got = ul.correlation_of(req, "direct")
    assert (got["physical_attempts"], got["upstream_responses"]) == (1, 0)
    assert got["provider_request_id"] is None


def test_central_relay_never_names_a_provider_id(capture_on):
    req = _Req((HEADER, _uid()))
    ul.note_physical_attempt(req)
    ul.note_response(req, _Up(request_id=PROVIDER_A))
    assert ul.correlation_of(req, "central")["provider_request_id"] is None


def test_no_account_reset_or_time_inference_fields(capture_on):
    req = _Req((HEADER, _uid()))
    got = ul.correlation_of(req, "direct")
    banned = ("account", "reset", "bearer", "epoch", "window", "time", "selected")
    assert not [k for k in got if any(b in k for b in banned)]


def test_response_list_is_bounded(capture_on):
    req = _Req()
    for _ in range(ul._MAX_RESPONSES + 5):
        ul.note_response(req, _Up(request_id=PROVIDER_A))
    assert len(req[ul.ATTEMPT_KEY]["responses"]) == ul._MAX_RESPONSES


# ------------------------------------------------- end to end through the real proxy


def _sse_headers(provider: str | None) -> dict[str, str]:
    headers = {"content-type": "text/event-stream"}
    if provider:
        headers["request-id"] = provider
    return headers


def _stub_upstream() -> web.Application:
    calls = {"retry": 0}

    async def messages(request: web.Request) -> web.StreamResponse:
        await request.read()
        mode = request.headers.get("X-Stub-Mode", "ok")
        if mode == "retry":
            calls["retry"] += 1
            if calls["retry"] == 1:
                return web.Response(
                    status=429,
                    headers={
                        "retry-after": "0",
                        "request-id": PROVIDER_A,
                        "anthropic-ratelimit-unified-status": "allowed",
                        "anthropic-ratelimit-unified-5h-status": "allowed",
                        "anthropic-ratelimit-unified-5h-utilization": "0.10",
                        "anthropic-ratelimit-unified-7d-status": "allowed",
                        "anthropic-ratelimit-unified-7d-utilization": "0.10",
                        "anthropic-ratelimit-unified-representative-claim": "five_hour",
                    },
                )
            provider = PROVIDER_B
        elif mode == "bad-id":
            provider = "req_x"
        elif mode == "slow":
            await asyncio.sleep(0.15)
            provider = request.headers.get("X-Stub-Provider")
        else:
            provider = request.headers.get("X-Stub-Provider", PROVIDER_A)
        response = web.StreamResponse(status=200, headers=_sse_headers(provider))
        await response.prepare(request)
        await response.write(_SSE_BODY)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_route("*", "/v1/messages", messages)
    return app


@pytest.fixture
async def gateway(monkeypatch, tmp_path, capture_on):
    ledger = tmp_path / "usage-ledger.jsonl"
    monkeypatch.setattr(ul, "default_path", lambda: ledger)
    upstream = TestServer(_stub_upstream())
    await upstream.start_server()
    for module in (limiter, pacing):
        module.set_lock(asyncio.Lock())
    overrides = {
        "UPSTREAM": str(upstream.make_url("")).rstrip("/"),
        "CENTRAL_URL": "",
        "QUEUE_MODE": "fair",
        "RATE_PUSHBACK_RETRIES": 1,
        "CONCURRENCY_COOLDOWN_S": 0.01,
        "MAX_HOLD_RETRY_AFTER_S": 1.0,
    }
    for name, value in overrides.items():
        monkeypatch.setattr(config, name, value)
    _reset_proxy_state()
    client = TestClient(TestServer(_make_proxy_app()))
    await client.start_server()
    client.ledger = ledger
    try:
        yield client
    finally:
        await client.close()
        await upstream.close()


async def _post(client: TestClient, bearer: str, headers: dict[str, str]) -> int:
    resp = await client.post(
        "/v1/messages",
        data=b'{"model":"claude-sonnet-5-5","max_tokens":1}',
        headers={"Authorization": f"Bearer {bearer}", **headers},
    )
    await resp.read()
    return resp.status


async def _rows(path, count: int, wait_s: float = 5.0) -> list[dict]:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            if len(rows) >= count:
                return rows
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {count} ledger rows")


async def test_clean_request_binds_claim_and_provider_id(gateway):
    value = _uid()
    assert await _post(gateway, "clean", {HEADER: value}) == 200
    (row,) = await _rows(gateway.ledger, 1)
    assert row["attempt_binding"] == "claimed" and row["x_pi_attempt_id"] == value
    assert (row["physical_attempts"], row["upstream_responses"]) == (1, 1)
    assert row["retry_observed"] is False
    assert row["provider_request_id"] == PROVIDER_A
    assert row["provider_request_ids"] == [PROVIDER_A]
    assert row["bearer"] and value not in row["bearer"]  # the account stays an opaque hash
    assert not any(k.startswith("reset") for k in row)  # reset provenance stays absent


async def test_gateway_retry_is_one_request_with_two_physical_attempts(gateway):
    value = _uid()
    assert await _post(gateway, "retrier", {HEADER: value, "X-Stub-Mode": "retry"}) == 200
    (row,) = await _rows(gateway.ledger, 1)
    assert row["attempt_binding"] == "claimed" and row["x_pi_attempt_id"] == value
    assert (row["physical_attempts"], row["upstream_responses"]) == (2, 2)
    assert row["retry_observed"] is True
    assert row["provider_request_ids"] == [PROVIDER_A, PROVIDER_B]
    assert row["provider_request_id"] is None  # two provider requests, one client claim: no join


async def test_missing_header_row_has_provider_id_but_no_claim(gateway):
    assert await _post(gateway, "anon", {}) == 200
    (row,) = await _rows(gateway.ledger, 1)
    assert row["attempt_binding"] == "missing" and row["x_pi_attempt_id"] is None
    assert row["provider_request_id"] == PROVIDER_A


async def test_bad_provider_id_is_not_recorded(gateway):
    assert await _post(gateway, "bad", {HEADER: _uid(), "X-Stub-Mode": "bad-id"}) == 200
    (row,) = await _rows(gateway.ledger, 1)
    assert row["provider_request_ids"] == [None] and row["provider_request_id"] is None
    assert "req_x" not in json.dumps(row)


async def test_overlapping_requests_keep_their_own_ids(gateway):
    ids = [_uid() for _ in range(6)]
    providers = [f"req_011CSoverlap{n:08d}" for n in range(6)]
    statuses = await asyncio.gather(
        *[
            _post(
                gateway,
                f"ov{n}",
                {HEADER: ids[n], "X-Stub-Mode": "slow", "X-Stub-Provider": providers[n]},
            )
            for n in range(6)
        ]
    )
    assert statuses == [200] * 6
    rows = await _rows(gateway.ledger, 6)
    by_claim = {row["x_pi_attempt_id"]: row for row in rows}
    assert set(by_claim) == set(ids)
    for n in range(6):
        assert by_claim[ids[n]]["provider_request_id"] == providers[n]
        assert by_claim[ids[n]]["physical_attempts"] == 1
    assert len({row["gateway_request_id"] for row in rows}) == 6


async def test_replay_across_requests_marks_the_second_row_only(gateway):
    value = _uid()
    await _post(gateway, "replay-1", {HEADER: value})
    await _post(gateway, "replay-2", {HEADER: value})
    first, second = await _rows(gateway.ledger, 2)
    assert first["attempt_binding"] == "claimed" and first["x_pi_attempt_id"] == value
    assert second["attempt_binding"] == "replayed" and second["x_pi_attempt_id"] is None


async def test_capture_off_leaves_rows_as_before(gateway, monkeypatch):
    monkeypatch.setattr(config, "ATTEMPT_ID_CAPTURE_MODE", "off")
    await _post(gateway, "plain", {HEADER: _uid()})
    (row,) = await _rows(gateway.ledger, 1)
    assert not ({"attempt_binding", "x_pi_attempt_id", "gateway_request_id"} & set(row))
    assert "physical_attempts" not in row
