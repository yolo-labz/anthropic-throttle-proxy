"""Upstream forwarding: target selection, central health polling, and the
single-attempt request/stream primitive.

These functions own the raw ``aiohttp.ClientSession`` hot path. No vendor AI
SDK is imported here (invariant #1).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from . import config
from .config import log
from .pacing import _pace_dispatch
from .ratelimit import _extract_ratelimit, _extract_zai_ratelimit_from_body

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

# Result of a single forward attempt: (response, status, captured, exc, meta).
ForwardResult = tuple[
    "web.StreamResponse | None",
    "int | None",
    "bytearray | None",
    "Exception | None",
    "dict[str, str] | None",
]


class RetryableStatusError(RuntimeError):
    """Upstream answered with a status the caller asked to treat as a failure.

    ``proxy_served`` is True when the response carried ``config.MARKER_HEADER``
    — a live central relayed an upstream 5xx — so the local tier must retry
    direct for this request but NOT mark central DOWN.
    """

    def __init__(self, message: str, proxy_served: bool = False) -> None:
        super().__init__(message)
        self.proxy_served = proxy_served


async def stamp_proxy_marker(_request: web.Request, response: web.StreamResponse) -> None:
    """``on_response_prepare`` hook: mark every response this proxy serves.

    Registered app-wide in ``proxy.main()`` so streamed relays, generated
    errors, and health responses all carry ``config.MARKER_HEADER``. See the
    constant's comment in config.py for why the local tier needs it.
    """
    response.headers[config.MARKER_HEADER] = "1"


def _target_url(base: str, path: str, query: str) -> str:
    url = f"{base}/{path}"
    return f"{url}?{query}" if query else url


def direct_target(path: str, query: str) -> tuple[str, aiohttp.ClientTimeout]:
    """Direct-upstream URL + timeout for this request, ignoring central."""
    timeout = aiohttp.ClientTimeout(total=None, sock_read=600, sock_connect=30)
    return _target_url(config.UPSTREAM, path, query), timeout


def pick_target(path: str, query: str) -> tuple[str, aiohttp.ClientTimeout, str]:
    """Choose upstream URL for this request.

    A configured central tier is preferred until it is explicitly marked down.
    Cold-starting as direct while the first health probe is still pending lets
    restart bursts bypass fleet-wide admission before central has a chance to
    answer, which recreates the rate-limit storm this proxy is meant to absorb.
    """
    if config.CENTRAL_URL and config.state["central_status"] != "down":
        client_timeout = aiohttp.ClientTimeout(
            total=None, sock_read=600, sock_connect=config.CENTRAL_FORWARD_TIMEOUT
        )
        return _target_url(config.CENTRAL_URL, path, query), client_timeout, "central"
    url, client_timeout = direct_target(path, query)
    return url, client_timeout, "direct"


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
            try:
                payload = await r.json(content_type=None)
            except Exception:
                payload = {}
            upstream_egress_ok = payload.get("upstream_egress_ok", True)
            if r.status == 200 and upstream_egress_ok:
                _record_central_sample(True)
            else:
                detail = payload.get("upstream_egress_error") or f"health returned {r.status}"
                _record_central_sample(False, str(detail))
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
    drop_headers = config.HOP_HEADERS
    if config.MARKER_HEADER not in upstream.headers:
        # Only a sibling proxy tier (marker-stamped) may assert the
        # queue-timeout contract. A raw upstream 429/503 carrying the header
        # verbatim would exempt REAL pushback from pushback-retry and AIMD
        # shrink (Codex MAJOR on PR #83). A double-spoof (marker + stamp) is
        # the same accepted trust boundary as the PR #81 marker itself.
        drop_headers = config.HOP_HEADERS | {config.QUEUE_TIMEOUT_HEADER}
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in drop_headers}
    meta = _extract_ratelimit(upstream.headers)
    if upstream.status in config.THROTTLE_STATUSES:
        body = await upstream.read()
        meta.update(
            _extract_zai_ratelimit_from_body(
                body,
                quota_jitter_s=config.ZAI_QUOTA_RESET_JITTER_S,
            )
        )
        captured = bytearray(body[: 1024 * 1024])
        return (
            web.Response(status=upstream.status, headers=resp_headers, body=body),
            (upstream.status),
            captured,
            None,
            meta,
        )
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


def _assert_allowed_upstream(url: str) -> None:
    """Reject any upstream URL whose host is not a CONFIGURED target.

    The proxy is a reverse proxy: ``_target_url`` appends the client's path and
    query to a FIXED operator-set base (``THROTTLE_UPSTREAM`` or
    ``THROTTLE_CENTRAL_URL``), so the request host is never client-controlled.
    Validating the host against that allowlist is defense-in-depth at the trust
    boundary AND the sanitizer that lets CodeQL confirm the client-influenced
    path/query cannot redirect the request to an arbitrary server (py/partial-ssrf).
    Raises ``ValueError`` — callers translate it to a normal forward failure.
    """
    host = urlsplit(url).hostname
    allowed = {urlsplit(config.UPSTREAM).hostname}
    if config.CENTRAL_URL:
        allowed.add(urlsplit(config.CENTRAL_URL).hostname)
    if host not in allowed:
        raise ValueError(f"refusing upstream request to non-configured host {host!r}")


@asynccontextmanager
async def _open_upstream(
    request: web.Request,
    headers: Mapping[str, str],
    body: bytes | None,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Issue ONE paced, host-validated upstream request; yield the response.

    The single place the proxy opens an upstream ``aiohttp`` request. Both the
    streaming forwarder (``_forward_once``) and the keepalive-hold retry loop
    (``proxy._forward_once_into_sse``) go through here, so there is exactly ONE
    guarded SSRF sink and no duplicated connector/session/pace/request block.
    """
    _assert_allowed_upstream(url)
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(
        timeout=client_timeout,
        connector=connector,
        auto_decompress=False,
    ) as session:
        # Burst-smoothing pace (no-op when THROTTLE_MIN_DISPATCH_GAP_MS=0).
        # Inside the session context so connector + TLS handshake set-up time
        # doesn't count against the gap budget — we pace the request issuance.
        await _pace_dispatch()
        async with session.request(
            request.method,
            url,
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as upstream:
            yield upstream


async def _forward_once(
    request: web.Request,
    headers: Mapping[str, str],
    body: bytes | None,
    url: str,
    client_timeout: aiohttp.ClientTimeout,
    retryable_statuses: set[int] | None = None,
) -> ForwardResult:
    """Forward request to URL and stream the response back.

    Distinguishes client-side disconnects from upstream errors so the caller
    can retry upstream failures but not waste cycles retrying after the client
    gave up. Returns ``(response, status, captured, None, meta)`` on success or
    ``(None, None, None, exc, None)`` on upstream failure, where ``meta`` is the
    extracted upstream rate-limit headers. Raises ConnectionResetError /
    ClientConnectionResetError on client-side disconnect.
    """
    try:
        async with _open_upstream(request, headers, body, url, client_timeout) as upstream:
            if retryable_statuses and upstream.status in retryable_statuses:
                payload = await upstream.read()
                snippet = payload[:500].decode("utf-8", "replace").strip()
                return (
                    None,
                    upstream.status,
                    bytearray(payload[: 1024 * 1024]),
                    RetryableStatusError(
                        f"retryable upstream status {upstream.status}: {snippet}",
                        proxy_served=config.MARKER_HEADER in upstream.headers,
                    ),
                    _extract_ratelimit(upstream.headers),
                )
            return await _stream_response(request, upstream)
    except aiohttp.ClientConnectionResetError:
        # Raised by StreamResponse.write/write_eof when the Claude client
        # closes its local socket while we are streaming. Let proxy.handler
        # record this as a client disconnect; treating it as an upstream or
        # central failure wastes a retry and can push the local proxy into
        # direct fallback under load.
        raise
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        # ValueError = _assert_allowed_upstream rejected a non-configured host;
        # surface it as a (permanent) upstream failure, not an unhandled 500.
        return None, None, None, exc, None
