"""Metadata only: real upstream identity, outcome and selected-account boundaries."""

import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from anthropic_throttle_proxy import config, forwarding, proxy
from anthropic_throttle_proxy import usage_ledger as ul

ID = "msg_UPSTREAM_123"
HASH = hashlib.sha256(ID.encode()).hexdigest()


def sse(*events):
    return b"".join(
        b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n"
        for e in events
    )


def start(identity=ID):
    return {
        "type": "message_start",
        "message": {
            "id": identity,
            "usage": {
                "input_tokens": 9,
                "output_tokens": 1,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 7,
            },
        },
    }


DELTA = {"type": "message_delta", "usage": {"output_tokens": 42}}
STOP = {"type": "message_stop"}


@pytest.fixture
def rows(monkeypatch):
    captured = []
    monkeypatch.setattr(config, "USAGE_LEDGER_MODE", "observe")
    monkeypatch.setattr(ul, "_enqueue", captured.append)
    return captured


def record(body, **kwargs):
    ul.record(
        bearer="authoritative-selected",
        model="claude-sonnet-5-5",
        project=None,
        status=kwargs.pop("status", 200),
        captured=body,
        **kwargs,
    )


@pytest.mark.parametrize("compressed", [False, True])
def test_upstream_id_hash_and_normal_terminal(rows, compressed):
    body = sse(start(), DELTA, STOP)
    record(gzip.compress(body) if compressed else body, eof=True, stream_error=False, via="direct")
    row = rows[0]
    assert row["response_id_hash"] == HASH
    assert row["terminal_seen"] and row["upstream_eof"]
    assert row["settlement"] == "normal"
    assert row["bearer"] == "authoritative-selected" and row["account_authoritative"]
    assert row["output"] == 42 and row["cacheRead"] == 30
    assert ID not in json.dumps(row)


def test_delta_without_stop_or_eof_is_not_a_normal_terminal(rows):
    record(sse(start(), DELTA))
    assert rows[0]["response_id_hash"] == HASH
    assert rows[0]["settlement"] == "unknown"
    assert rows[0]["upstream_eof"] is None


def test_abort_retains_observed_possibly_billed_usage_not_useful_terminal(rows):
    record(sse(start()), status=499, eof=False, stream_error=True, via="direct")
    assert rows[0]["response_id_hash"] == HASH
    assert rows[0]["input"] == 9 and rows[0]["cacheWrite"] == 7
    assert rows[0]["usage_complete"] is False
    assert rows[0]["settlement"] == "error"


def test_duplicate_events_do_not_multiply_or_authorize(rows):
    record(sse(start(), start(), DELTA, DELTA, STOP), eof=True, stream_error=False)
    assert rows[0]["response_id_hash"] == HASH and rows[0]["output"] == 42
    record(sse(start(), start("msg_DIFFERENT"), DELTA, STOP), eof=True, stream_error=False)
    assert rows[1]["response_id_hash"] is None and rows[1]["settlement"] == "unknown"


def test_error_event_overrides_200_and_stop(rows):
    record(
        sse(start(), DELTA, STOP, {"type": "error", "error": {"message": "PRIVATE BODY"}}),
        eof=True,
        stream_error=False,
    )
    assert rows[0]["settlement"] == "error"
    assert "PRIVATE BODY" not in json.dumps(rows)


def test_central_local_bearer_is_not_claimed_as_selected_upstream(rows):
    record(sse(start(), DELTA, STOP), eof=True, stream_error=False, via="central")
    assert rows[0]["response_id_hash"] == HASH
    assert rows[0]["bearer"] is None and not rows[0]["account_authoritative"]
    assert rows[0]["account_scope"] == "unknown_central"


class Content:
    def __init__(self, chunks, fail=False):
        self.chunks, self.fail, self.finished = chunks, fail, False

    async def iter_any(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise OSError("synthetic upstream reset")
        self.finished = True

    def at_eof(self):
        return self.finished


class Sink:
    def __init__(self, *args, **kwargs):
        self.body = bytearray()

    async def prepare(self, request):
        pass

    async def write(self, chunk):
        self.body.extend(chunk)

    async def write_eof(self):
        pass


def record_observed(observed, status=200):
    record(
        observed["captured"],
        status=status,
        tail=observed["tail"],
        eof=observed["eof"],
        stream_error=observed["error"],
        via=observed["via"],
    )


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
async def test_real_fragmented_transport_capture_and_abort(rows, monkeypatch, held, compressed):
    # Held streams are already decoded by their existing auto_decompress=True transport.
    original = sse(start(), DELTA, STOP)
    wire = gzip.compress(original) if compressed and not held else original
    chunks = [wire[i : i + 3] for i in range(0, len(wire), 3)]
    request = make_mocked_request(
        "POST",
        "/v1/messages",
        headers={"x-response-id": "msg_CLIENT_FAKE", "x-selected-account": "client-fake"},
    )
    monkeypatch.setattr(config, "UPSTREAM", "https://api.example.test")
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(forwarding.web, "StreamResponse", Sink)
    monkeypatch.setattr(
        forwarding.prospective_runtime, "get_runtime", lambda request: SimpleNamespace(mode="off")
    )
    sent_headers = {"Authorization": "Bearer OFFLINE-selected-account"}
    upstream = SimpleNamespace(
        status=200,
        headers={},
        url="https://api.example.test/v1/messages",
        content=Content(chunks),
        request_info=SimpleNamespace(headers=sent_headers),
    )
    if held:
        sink = Sink()
        await proxy._pipe_sse_upstream(request, upstream, sink, None)
    else:
        sink, *_ = await forwarding._stream_response(request, upstream)
    assert bytes(sink.body) == wire
    observed = request[ul.CAPTURE_KEY]
    assert (
        observed["bearer"] == hashlib.sha256(sent_headers["Authorization"].encode()).hexdigest()[:8]
    )
    record_observed(observed)
    assert rows[-1]["response_id_hash"] == HASH and rows[-1]["settlement"] == "normal"
    assert "client-fake" not in json.dumps(rows[-1])
    # Failure after a real start must leave metadata even when held caller raises.
    upstream.content = Content([sse(start())], fail=True)
    if held:
        with pytest.raises(OSError):
            await proxy._pipe_sse_upstream(request, upstream, Sink(), None)
    else:
        await forwarding._stream_response(request, upstream)
    observed = request[ul.CAPTURE_KEY]
    record_observed(observed, status=499)
    assert rows[-1]["response_id_hash"] == HASH and rows[-1]["settlement"] == "error"
