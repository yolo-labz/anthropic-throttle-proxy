"""Queue-wait bound (PR #83): a request parked past QUEUE_MAX_WAIT_S fails
fast with a clean 503 + Retry-After instead of stalling past the client's
socket timeout.

Failure path being pinned: 07/07/2026 — only 2/5 bearers usable → 60-80 s
queue waits → claude aborted its socket at ~60 s → the proxy's late write hit
a closing transport → the client saw truncated HTTP → Node fetch raised
``InvalidHTTPResponse`` → claude-code misread it as a 401 login failure.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter, QueueWaitTimeout


async def test_slot_max_wait_times_out_clean(monkeypatch) -> None:
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 1)
    lim = FairBearerLimiter(1, "fair")
    await lim.acquire("holder")
    with pytest.raises(QueueWaitTimeout):
        async with lim.slot("waiter", max_wait=0.05):
            raise AssertionError("slot must not dispatch while the pool is full")
    snap = lim.snapshot()
    # The wait_for cancellation ran acquire's _cancel_cleanup: the parked
    # future is gone, no slot was consumed, and nothing shrank.
    assert snap["queued_total"] == 0
    assert snap["inflight"] == 1
    assert snap["max_concurrent"] == 1
    await lim.release()
    assert lim.snapshot()["inflight"] == 0


async def _hold_and_park(max_wait: float | None) -> tuple[FairBearerLimiter, asyncio.Task]:
    """One-slot fair limiter with its slot held + a waiter task parked on it."""
    lim = FairBearerLimiter(1, "fair")
    await lim.acquire("holder")

    async def waiter() -> bool:
        async with lim.slot("waiter", max_wait=max_wait):
            return True

    return lim, asyncio.create_task(waiter())


async def test_slot_max_wait_dispatches_before_deadline(monkeypatch) -> None:
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 1)
    lim, task = await _hold_and_park(max_wait=5.0)
    for _ in range(50):
        if lim.snapshot()["queued_total"] == 1:
            break
        await asyncio.sleep(0.01)
    assert lim.snapshot()["queued_total"] == 1
    await lim.release()
    assert await asyncio.wait_for(task, timeout=1.0) is True
    assert lim.snapshot()["inflight"] == 0


async def test_slot_unbounded_when_max_wait_none(monkeypatch) -> None:
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 1)
    lim, task = await _hold_and_park(max_wait=None)
    await asyncio.sleep(0.05)
    assert not task.done()
    await lim.release()
    assert await asyncio.wait_for(task, timeout=1.0) is True


async def test_non_queue_mode_ignores_max_wait() -> None:
    lim = FairBearerLimiter(1, "off")
    async with lim.slot("c1", max_wait=0.001):
        # off mode acquires instantly regardless of pool depth, so the bound
        # must not wrap the acquire in timeout machinery.
        async with lim.slot("c2", max_wait=0.001):
            assert lim.inflight == 2


def test_queue_timeout_response_shape() -> None:
    lim = FairBearerLimiter(1, "fair")
    resp = proxy._queue_wait_timeout_response("bid12345", "cid", "v1/messages", lim)
    assert resp.status == 503
    assert resp.headers["retry-after"] == str(config.QUEUE_TIMEOUT_RETRY_AFTER_S)
    assert resp.headers[config.QUEUE_TIMEOUT_HEADER] == "1"


def test_should_retry_pushback_exempts_queue_timeout() -> None:
    attempt = proxy._Attempt()
    attempt.final_status = 503
    plain = web.Response(status=503)
    stamped = web.Response(status=503, headers={config.QUEUE_TIMEOUT_HEADER: "1"})
    assert proxy._should_retry_pushback(plain, attempt, 0) is True
    assert proxy._should_retry_pushback(stamped, attempt, 0) is False


async def test_aimd_feedback_skips_queue_timeout_relay(monkeypatch) -> None:
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 3)
    lim = FairBearerLimiter(3, "fair")
    attempt = proxy._Attempt()
    attempt.final_status = 503
    attempt.response = web.Response(status=503, headers={config.QUEUE_TIMEOUT_HEADER: "1"})
    await proxy._aimd_feedback("bid", lim, attempt)
    # Relayed central admission backpressure must not shrink this bearer.
    assert lim.max_concurrent == 3
    attempt.response = web.Response(status=503)
    await proxy._aimd_feedback("bid", lim, attempt)
    # A real upstream 503 still shrinks.
    assert lim.max_concurrent < 3
