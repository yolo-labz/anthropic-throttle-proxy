"""Upstream forwarding: target selection, central health polling, and the
single-attempt request/stream primitive.

These functions own the raw ``aiohttp.ClientSession`` hot path. No vendor AI
SDK is imported here (invariant #1).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from . import config
from .config import log
from .pacing import _pace_dispatch
from .ratelimit import _extract_ratelimit

if TYPE_CHECKING:
    from collections.abc import Mapping

# Result of a single forward attempt: (response, status, captured, exc, meta).
ForwardResult = tuple[
    "web.StreamResponse | None",
    "int | None",
    "bytearray | None",
    "Exception | None",
    "dict[str, str] | None",
]


def pick_target(path: str, query: str) -> tuple[str, aiohttp.ClientTimeout, str]:
    """Choose upstream URL for this request: central if healthy, else direct."""
    if config.CENTRAL_URL and config.state["central_status"] == "up":
        base = config.CENTRAL_URL
        client_timeout = aiohttp.ClientTimeout(
            total=None, sock_read=600, sock_connect=config.CENTRAL_FORWARD_TIMEOUT
        )
        via = "central"
    else:
        base = config.UPSTREAM
        client_timeout = aiohttp.ClientTimeout(total=None, sock_read=600, sock_connect=30)
        via = "direct"
    url = f"{base}/{path}"
    if query:
        url += "?" + query
    return url, client_timeout, via


def _record_central_sample(healthy: bool, detail: str = "") -> None:
    """Fold one probe result into the hysteresis counters and flip status only
    once a consecutive-sample threshold is crossed.

    A single transient miss no longer abandons central (which would flip the
    whole local fleet to direct fallback); it takes ``CENTRAL_HEALTH_FAIL_THRESHOLD``
    consecutive failures to go DOWN and ``CENTRAL_HEALTH_OK_THRESHOLD`` consecutive
    successes to go UP. Logs only on an actual transition so steady state is quiet.
    """
    st = config.state
    if healthy:
        st["central_consecutive_ok"] = int(st.get("central_consecutive_ok", 0)) + 1
        st["central_consecutive_fail"] = 0
        if st["central_status"] == "up":
            return
        # Cold start ("unknown") adopts central on the FIRST healthy probe. There is
        # no established relationship to protect, and defaulting to direct upstream
        # during startup/restart recreates the unqueued-upstream firehose this
        # hysteresis exists to prevent (Codex review, PR #35). The OK_THRESHOLD
        # re-adoption delay applies ONLY when recovering from a DOWN — there we
        # don't trust a flapping central immediately.
        ok_needed = config.CENTRAL_HEALTH_OK_THRESHOLD if st["central_status"] == "down" else 1
        if st["central_consecutive_ok"] >= ok_needed:
            log(f"central {config.CENTRAL_URL} is UP (after {st['central_consecutive_ok']} ok)")
            st["central_status"] = "up"
    else:
        st["central_consecutive_fail"] = int(st.get("central_consecutive_fail", 0)) + 1
        st["central_consecutive_ok"] = 0
        if (
            st["central_status"] != "down"
            and st["central_consecutive_fail"] >= config.CENTRAL_HEALTH_FAIL_THRESHOLD
        ):
            log(f"central DOWN: {detail} (after {st['central_consecutive_fail']} fails)")
            st["central_status"] = "down"


async def _poll_central_once(session: aiohttp.ClientSession) -> None:
    """One central health probe; fold the result into the hysteresis counters.

    Never raises — a failed probe is recorded as an unhealthy sample.
    """
    try:
        async with session.get(config.CENTRAL_URL + config.CENTRAL_HEALTH_PATH) as r:
            if r.status == 200:
                await r.read()
                _record_central_sample(True)
            else:
                _record_central_sample(False, f"health returned {r.status}")
    except Exception as exc:
        _record_central_sample(False, f"unreachable: {exc!r}")


async def central_health_loop() -> None:
    """Background poll of central /__throttle/health. Updates state."""
    if not config.CENTRAL_URL:
        return
    timeout = aiohttp.ClientTimeout(total=config.CENTRAL_HEALTH_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await _poll_central_once(session)
            config.state["central_last_check"] = time.time()
            await asyncio.sleep(config.CENTRAL_HEALTH_INTERVAL)


async def _stream_response(request: web.Request, upstream: aiohttp.ClientResponse) -> ForwardResult:
    """Stream an open upstream response back to the client, capturing usage.

    Tees up to 1 MiB of the body into a buffer so the caller can scan the SSE
    ``usage`` block after the client finishes reading — the usage block lives
    in the final few KB of any claude response, even for long generations.
    """
    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in config.HOP_HEADERS
    }
    meta = _extract_ratelimit(upstream.headers)
    response = web.StreamResponse(status=upstream.status, headers=resp_headers)
    await response.prepare(request)
    captured = bytearray()
    cap_limit = 1024 * 1024
    async for chunk in upstream.content.iter_any():
        if not chunk:
            break
        await response.write(chunk)
        if len(captured) < cap_limit:
            captured.extend(chunk[: cap_limit - len(captured)])
    await response.write_eof()
    return response, upstream.status, captured, None, meta


async def _forward_once(
    request: web.Request,
    headers: Mapping[str, str],
    body: bytes | None,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
) -> ForwardResult:
    """Forward request to URL and stream the response back.

    Distinguishes client-side disconnects from upstream errors so the caller
    can retry upstream failures but not waste cycles retrying after the client
    gave up. Returns ``(response, status, captured, None, meta)`` on success or
    ``(None, None, None, exc, None)`` on upstream failure, where ``meta`` is the
    extracted upstream rate-limit headers. Raises ConnectionResetError /
    ClientConnectionResetError on client-side disconnect.
    """
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(
        timeout=client_timeout,
        connector=connector,
        auto_decompress=False,
    ) as session:
        # Burst-smoothing pace (no-op when THROTTLE_MIN_DISPATCH_GAP_MS=0).
        # Placed inside the session context so the connector + TLS handshake
        # set-up time doesn't count against the gap budget — we pace the
        # actual upstream request issuance, not the prep.
        await _pace_dispatch()
        try:
            async with session.request(
                request.method,
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
            ) as upstream:
                return await _stream_response(request, upstream)
        except (TimeoutError, aiohttp.ClientError) as exc:
            return None, None, None, exc, None
