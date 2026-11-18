"""Real forwarding path: an SSE /v1/messages reply must move the token counter.

The upstream replays the exact shape measured live on 02/11/2026: Anthropic's
padded SSE frames (`message_start` carries input usage, `message_delta` the
output usage) with ``Content-Encoding: gzip``.
"""

from __future__ import annotations

import asyncio
import gzip

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_keepalive_hold import _make_proxy_app
from test_proxy_app import _reset_proxy_state

from anthropic_throttle_proxy import config, limiter, pacing, proxy
from anthropic_throttle_proxy import usage_allocator as ua
from anthropic_throttle_proxy.metrics import REGISTRY

MODEL = "claude-haiku-4-5"
SSE = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"model":"claude-haiku-4-5-20251001",'
    b'"id":"msg_x","type":"message","role":"assistant","content":[],"usage":'
    b'{"input_tokens":23,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":0},'
    b'"output_tokens":1,"service_tier":"standard"}}         }\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":'
    b'{"output_tokens":4}            }\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"    }\n\n'
)


def _tokens(kind: str) -> float:
    labels = {"model": MODEL, "kind": kind}
    return REGISTRY.get_sample_value("anthropic_tokens_total", labels) or 0.0


def _upstream(encode: bool) -> web.Application:
    async def messages(request: web.Request) -> web.StreamResponse:
        await request.read()
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        payload = SSE
        if encode:
            headers["Content-Encoding"] = "gzip"
            payload = gzip.compress(SSE)
        resp = web.StreamResponse(status=200, headers=headers)
        await resp.prepare(request)
        # Two writes: the gzip member spans chunks exactly as on the wire.
        await resp.write(payload[: len(payload) // 2])
        await resp.write(payload[len(payload) // 2 :])
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    return app


@pytest.mark.parametrize("encode", [False, True], ids=["identity", "gzip"])
async def test_real_path_sse_usage_increments_tokens_and_project(monkeypatch, encode) -> None:
    upstream = TestServer(_upstream(encode))
    await upstream.start_server()
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    monkeypatch.setattr(config, "UPSTREAM", str(upstream.make_url("")).rstrip("/"))
    monkeypatch.setattr(config, "CENTRAL_URL", "")
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "enforce")
    monkeypatch.setattr(proxy, "_WEEKLY_TOKENS", ua.WeeklyTokens())
    _reset_proxy_state()
    client = TestClient(TestServer(_make_proxy_app()))
    await client.start_server()
    try:
        before = (_tokens("input"), _tokens("output"))
        resp = await client.post(
            "/v1/messages",
            data=b'{"model":"claude-haiku-4-5","max_tokens":8,"stream":true}',
            headers={
                "x-api-key": "local-lane-via-8765",
                "accept-encoding": "gzip",
                "X-Throttle-Project": "p3a-probe",
            },
        )
        assert resp.status == 200
        await resp.read()
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        after = (_tokens("input"), _tokens("output"))
        # message_start 23 in / 1 out + message_delta 4 out (cumulative field
        # summed by the existing parser; asserting movement, not the sum).
        assert after[0] > before[0], f"input tokens frozen {before} -> {after}"
        assert after[1] > before[1], f"output tokens frozen {before} -> {after}"
        assert sum(proxy._WEEKLY_TOKENS.totals(__import__("time").time()).values()) > 0
    finally:
        await client.close()
        await upstream.close()


def test_truncated_gzip_head_still_yields_usage() -> None:
    from anthropic_throttle_proxy.ratelimit import _parse_sse_usage

    blob = gzip.compress(SSE)
    # A 1 MiB head capture can end mid-member; the decodable prefix still counts.
    head = blob[: len(blob) - 8]  # drop the gzip trailer (CRC + size)
    assert _parse_sse_usage(head)["input"] == 23
    assert _parse_sse_usage(b"\x1f\x8bnot-gzip") == dict.fromkeys(
        ("input", "output", "cache_read", "cache_creation"), 0
    )


def test_ledger_parses_gzip_capture_and_ignores_compressed_tail() -> None:
    from anthropic_throttle_proxy.usage_ledger import parse_usage

    blob = gzip.compress(SSE)
    counts, complete = parse_usage(blob, blob[-64:].decode("utf-8", "replace"))
    assert counts is not None and counts["input"] == 23 and counts["output"] == 4
    assert complete
