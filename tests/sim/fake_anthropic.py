"""Small fake Anthropic upstream for local load and routing rehearsals.

Run:
    uv run python tests/sim/fake_anthropic.py

The server enforces an artificial per-bearer concurrency cap and returns the
same broad classes of responses the proxy must survive: normal JSON responses,
streaming SSE responses, and 429s with unified-window headers.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web

HOST = os.environ.get("FAKE_ANTHROPIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("FAKE_ANTHROPIC_PORT", "9000"))
MAX_PER_BEARER = max(1, int(os.environ.get("FAKE_ANTHROPIC_MAX_PER_BEARER", "1")))
DELAY_MS = max(0, int(os.environ.get("FAKE_ANTHROPIC_DELAY_MS", "750")))
RETRY_AFTER = max(0, int(os.environ.get("FAKE_ANTHROPIC_RETRY_AFTER", "2")))
REJECT_AFTER = max(0, int(os.environ.get("FAKE_ANTHROPIC_REJECT_AFTER", "0")))

_inflight: defaultdict[str, int] = defaultdict(int)
_served: defaultdict[str, int] = defaultdict(int)
_lock = asyncio.Lock()


def _bearer_id(request: web.Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip()[:16] or "anonymous"


def _usage_headers(bid: str, *, rejected: bool = False) -> dict[str, str]:
    status = "rejected" if rejected else "allowed"
    util = "1" if rejected else "0.42"
    reset = str(int(time.time()) + 300)
    return {
        "anthropic-ratelimit-unified-status": status,
        "anthropic-ratelimit-unified-status-5h": status,
        "anthropic-ratelimit-unified-status-7d": "allowed",
        "anthropic-ratelimit-unified-utilization": util,
        "anthropic-ratelimit-unified-utilization-5h": util,
        "anthropic-ratelimit-unified-utilization-7d": "0.5",
        "anthropic-ratelimit-unified-reset": reset,
        "anthropic-ratelimit-unified-reset-5h": reset,
        "anthropic-ratelimit-unified-reset-7d": str(int(time.time()) + 86400),
        "x-fake-bearer": bid,
    }


@asynccontextmanager
async def _slot(bid: str) -> AsyncIterator[bool]:
    async with _lock:
        if _inflight[bid] >= MAX_PER_BEARER:
            yield False
            return
        _inflight[bid] += 1
    try:
        yield True
    finally:
        async with _lock:
            _inflight[bid] -= 1
            if _inflight[bid] <= 0:
                _inflight.pop(bid, None)


async def health(_request: web.Request) -> web.Response:
    async with _lock:
        return web.json_response(
            {
                "ok": True,
                "max_per_bearer": MAX_PER_BEARER,
                "inflight": dict(_inflight),
                "served": dict(_served),
            }
        )


async def messages(request: web.Request) -> web.StreamResponse:
    bid = _bearer_id(request)
    async with _slot(bid) as accepted:
        if not accepted:
            headers = _usage_headers(bid)
            if RETRY_AFTER:
                headers["retry-after"] = str(RETRY_AFTER)
            return web.json_response(
                {"type": "error", "error": {"type": "rate_limit_error", "message": "fake cap"}},
                status=429,
                headers=headers,
            )
        async with _lock:
            _served[bid] += 1
            served = _served[bid]
        rejected = bool(REJECT_AFTER and served >= REJECT_AFTER)
        if rejected:
            return web.json_response(
                {
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "fake rejected window"},
                },
                status=429,
                headers=_usage_headers(bid, rejected=True),
            )
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        await asyncio.sleep(DELAY_MS / 1000)
        headers = _usage_headers(bid)
        if body.get("stream"):
            response = web.StreamResponse(
                status=200,
                headers={**headers, "content-type": "text/event-stream"},
            )
            await response.prepare(request)
            await response.write(
                b'event: message_start\ndata: {"type":"message_start","message":{"id":"fake"}}\n\n'
            )
            await response.write(b'event: content_block_delta\ndata: {"delta":{"text":"ok"}}\n\n')
            await response.write(b"event: message_stop\ndata: {}\n\n")
            await response.write_eof()
            return response
        return web.json_response(
            {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "fake-model"),
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 8, "output_tokens": 1},
            },
            headers=headers,
        )


def app() -> web.Application:
    application = web.Application()
    application.router.add_get("/__fake/health", health)
    application.router.add_post("/v1/messages", messages)
    return application


if __name__ == "__main__":
    web.run_app(app(), host=HOST, port=PORT)
