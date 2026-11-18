"""Patient queue wait: ``x-anthropic-throttle-client-patience-ms`` lifts one
request's fair-queue bound from QUEUE_MAX_WAIT_S up to QUEUE_MAX_PATIENT_WAIT_S.

Clients without the header (Claude Code, ~60 s socket patience) keep the
historical 30 s bound byte-for-byte. Patient callers (pi) may wait longer; the
header is never forwarded upstream and is clamped to the operator ceiling.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import make_mocked_request

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter, QueueWaitTimeout


@pytest.fixture
def bounds(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_MAX_WAIT_S", 0.2)
    monkeypatch.setattr(config, "QUEUE_MAX_PATIENT_WAIT_S", 900.0)
    monkeypatch.setitem(config.state, "queue_patient_waiting", 0)
    proxy._QUEUE_WAIT_SAMPLES.clear()
    return monkeypatch


def test_no_header_keeps_the_historical_bound(bounds) -> None:
    """(a) Without the header the bound is exactly QUEUE_MAX_WAIT_S, unchanged."""
    assert proxy._effective_queue_max_wait({}) == 0.2
    assert proxy._effective_queue_max_wait({"x-unrelated": "9"}) == 0.2
    for junk in ("", "abc", "nan", "1e400", "0", "-5"):
        assert proxy._effective_queue_max_wait({config.PATIENCE_HEADER: junk}) == 0.2, junk


def test_header_raises_the_bound_and_is_clamped(bounds) -> None:
    """(c) The header raises the bound; anything above the knob is clamped."""
    assert proxy._effective_queue_max_wait({config.PATIENCE_HEADER: "600000"}) == 600.0
    assert proxy._effective_queue_max_wait({config.PATIENCE_HEADER: "2000000"}) == 900.0
    # Raise-only: a short ask never lowers the operator's default bound.
    assert proxy._effective_queue_max_wait({config.PATIENCE_HEADER: "5000"}) == 5.0
    assert proxy._effective_queue_max_wait({config.PATIENCE_HEADER: "50"}) == 0.2


def test_disabled_default_bound_stays_unbounded(bounds) -> None:
    """QUEUE_MAX_WAIT_S=0 means unbounded; a patience header must not bound it."""
    bounds.setattr(config, "QUEUE_MAX_WAIT_S", 0.0)
    assert proxy._effective_queue_max_wait({config.PATIENCE_HEADER: "600000"}) is None


def test_inherited_budget_still_caps_a_patient_request(bounds) -> None:
    """The end-to-end rule holds: min(own patient bound, inherited budget)."""
    eff = proxy._effective_queue_max_wait(
        {config.PATIENCE_HEADER: "600000", config.WAIT_BUDGET_HEADER: "2000"}
    )
    assert eff == 2.0


async def test_patient_request_waits_and_is_served(bounds) -> None:
    """(b) A 120 s backlog in miniature: the default bound would expire, the
    patient bound admits the waiter and it is served without a timeout.

    Mutant check: if the header were ignored, max_wait stays 0.2 s < 0.5 s
    holder time and this raises QueueWaitTimeout.
    """
    lim = FairBearerLimiter(1, "fair")
    await lim.acquire("holder")
    max_wait = proxy._effective_queue_max_wait({config.PATIENCE_HEADER: "3000"})

    async def release_later() -> None:
        await asyncio.sleep(0.5)
        await lim.release()

    releaser = asyncio.create_task(release_later())
    async with lim.slot("patient", max_wait=max_wait):
        served = True
    await releaser
    assert served
    assert lim.snapshot()["inflight"] == 0


async def test_default_bound_still_times_out_the_same_backlog(bounds) -> None:
    """Control for (b): the same backlog without the header gets QueueWaitTimeout."""
    lim = FairBearerLimiter(1, "fair")
    await lim.acquire("holder")
    max_wait = proxy._effective_queue_max_wait({})

    async def release_later() -> None:
        await asyncio.sleep(0.5)
        await lim.release()

    releaser = asyncio.create_task(release_later())
    with pytest.raises(QueueWaitTimeout):
        async with lim.slot("waiter", max_wait=max_wait):
            raise AssertionError("must not dispatch under the default bound")
    await releaser


def test_admission_judges_against_the_patient_bound(bounds) -> None:
    """(e) The depth estimate reads the request's own bound: a backlog that is
    undrainable in 30 s can still be admitted under the patient 900 s bound."""
    from anthropic_throttle_proxy.limiter import _compute_drain

    common = dict(
        slots=2,
        busy=2,
        queued=3,
        ahead=3,
        service_time_s=113.7,
        residuals=[113.7, 113.7],
        samples=8,
        source="measured",
        evidenced=True,
    )
    assert _compute_drain(**common, max_wait=30.0).rejects is True
    patient = proxy._effective_queue_max_wait({config.PATIENCE_HEADER: "900000"})
    estimate = _compute_drain(**common, max_wait=patient)
    assert estimate.max_wait_s == 900.0
    assert estimate.rejects is False


def test_header_is_never_forwarded_upstream(bounds) -> None:
    """(d) The patience header is consumed locally and stripped from forwards."""
    req = make_mocked_request(
        "POST",
        "/v1/messages",
        headers={
            config.PATIENCE_HEADER: "600000",
            config.WAIT_BUDGET_HEADER: "1000",
            "x-keep": "1",
        },
    )
    forwarded = {k.lower() for k in proxy._forward_headers(req)}
    assert config.PATIENCE_HEADER not in forwarded
    assert config.WAIT_BUDGET_HEADER not in forwarded
    assert "x-keep" in forwarded


async def test_mark_counts_patient_waiters_and_samples_served_waits(bounds) -> None:
    """Health publishes queue.patient_waiting and the wait percentiles."""
    mark = proxy._QueueWaitMark(patient=True)
    assert config.state["queue_patient_waiting"] == 1
    mark.end(served=True)
    mark.end(served=True)  # idempotent: no double decrement, no double sample
    assert config.state["queue_patient_waiting"] == 0
    assert len(proxy._QUEUE_WAIT_SAMPLES) == 1

    quiet = proxy._QueueWaitMark(patient=False)
    quiet.end(served=False)
    assert config.state["queue_patient_waiting"] == 0
    view = proxy._queue_health()
    assert view["patient_waiting"] == 0
    assert view["wait_samples"] == 1
    assert view["wait_p50_s"] is not None and view["wait_p95_s"] is not None
