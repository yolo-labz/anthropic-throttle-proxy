"""Pushback failover — the generic pushback hands the request back to the
routing gate (Active Routing S1, docs/ACTIVE-ROUTING-DESIGN-2026-10-09.md).

Contract under test:

* generic pushback + account routing ON  -> raise ``_RetryAfterArmed`` (the
  gate reroutes to a healthier sibling; none-better falls back to the same
  bearer with the latched window), latch the pause BEFORE the raise, and leave
  AIMD/metrics to the handler's finalize exactly once;
* generic pushback + account routing OFF -> unchanged same-bearer
  ``_aimd_feedback`` + ``wait_retry_after`` + retry;
* the armed-long-Retry-After branch is unchanged in both modes;
* the raise escapes ``_forward_with_retry`` instead of looping same-bearer.
"""

from __future__ import annotations

import time
from typing import Any, cast

import pytest
from aiohttp import web

from anthropic_throttle_proxy import config, proxy


class _StubLimiter:
    """Records the two window calls `_pushback_retry_step` may make."""

    def __init__(self) -> None:
        self.noted: list[float] = []
        self.waited = 0

    def note_retry_after(self, seconds: float) -> float:
        self.noted.append(seconds)
        return seconds

    async def wait_retry_after(self) -> None:
        self.waited += 1


def _attempt(status: int = 429) -> proxy._Attempt:
    attempt = proxy._Attempt()
    attempt.final_status = status
    attempt.meta = {}
    attempt.started_at = time.time()
    return attempt


async def _recording_aimd(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_aimd(bid: str, limiter: Any, attempt: Any) -> None:
        calls.append(bid)

    monkeypatch.setattr(proxy, "_aimd_feedback", fake_aimd)
    return calls


async def test_generic_pushback_hands_back_to_routing_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing ON + short pushback -> reroute hand-back, pause latched, no AIMD here."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30.0)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_pushback_pause", lambda _meta, _bid: (2.0, True))
    aimd_calls = await _recording_aimd(monkeypatch)
    lim = _StubLimiter()

    with pytest.raises(proxy._RetryAfterArmed) as excinfo:
        await proxy._pushback_retry_step("aaaa1111", cast(Any, lim), "v1/messages", _attempt(), 0)

    assert excinfo.value.remaining == 2.0
    # The window is latched BEFORE the hand-back so the gate cannot instantly
    # re-probe the same bearer, and the none-better fallback honors it.
    assert lim.noted == [2.0]
    # AIMD + metrics are the handler's finalize's job on this path (once).
    assert aimd_calls == []
    assert lim.waited == 0


async def test_generic_pushback_latches_synthetic_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic (header-less) pause is latched too: an unlatched pause could
    ping-pong between two pushing-back bearers past the per-call retry budget."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30.0)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_pushback_pause", lambda _meta, _bid: (30.0, True))
    await _recording_aimd(monkeypatch)
    lim = _StubLimiter()

    with pytest.raises(proxy._RetryAfterArmed):
        await proxy._pushback_retry_step(
            "bbbb2222", cast(Any, lim), "v1/messages", _attempt(503), 1
        )

    assert lim.noted == [30.0]


async def test_generic_pushback_same_bearer_wait_without_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing OFF is unchanged: AIMD + wait + same-bearer retry, no hand-back."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30.0)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: False)
    monkeypatch.setattr(proxy, "_pushback_pause", lambda _meta, _bid: (2.0, True))
    aimd_calls = await _recording_aimd(monkeypatch)
    lim = _StubLimiter()

    fast_fail = await proxy._pushback_retry_step(
        "cccc3333", cast(Any, lim), "v1/messages", _attempt(), 0
    )

    assert fast_fail is None
    assert aimd_calls == ["cccc3333"]
    assert lim.waited == 1
    assert lim.noted == []


async def test_armed_long_retry_after_branch_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long Retry-After: routing ON still raises+latches (as before S1)."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30.0)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_pushback_pause", lambda _meta, _bid: (120.0, False))
    aimd_calls = await _recording_aimd(monkeypatch)
    lim = _StubLimiter()

    with pytest.raises(proxy._RetryAfterArmed) as excinfo:
        await proxy._pushback_retry_step("dddd4444", cast(Any, lim), "v1/messages", _attempt(), 0)

    assert excinfo.value.remaining == 120.0
    assert lim.noted == [120.0]
    assert aimd_calls == []


async def test_armed_long_retry_after_fast_fail_relays_without_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long Retry-After + routing OFF: the fast-fail response is relayed (unchanged)."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30.0)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: False)
    monkeypatch.setattr(proxy, "_pushback_pause", lambda _meta, _bid: (120.0, False))
    aimd_calls = await _recording_aimd(monkeypatch)
    lim = _StubLimiter()

    fast_fail = await proxy._pushback_retry_step(
        "eeee5555", cast(Any, lim), "v1/messages", _attempt(), 0
    )

    assert isinstance(fast_fail, web.Response)
    assert fast_fail.status == 429
    assert fast_fail.headers.get("retry-after") == "120"
    assert aimd_calls == []
    assert lim.noted == []


async def test_forward_with_retry_escalates_pushback_to_reroute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic pushback inside `_forward_with_retry` does NOT loop on the same
    bearer: the hand-back exception escapes to the handler's dispatch loop."""
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 30.0)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_pushback_pause", lambda _meta, _bid: (2.0, True))
    await _recording_aimd(monkeypatch)

    pushed = web.Response(status=429, text="rate limited")

    async def fake_forward_or_recover(**_kw: Any) -> tuple[Any, bool]:
        return pushed, True

    monkeypatch.setattr(proxy, "_forward_or_recover", fake_forward_or_recover)
    monkeypatch.setattr(proxy, "_keepalive_hold_engages", lambda *_a, **_kw: False)
    monkeypatch.setattr(proxy, "_pushback_retry_engages", lambda *_a, **_kw: True)
    monkeypatch.setattr(proxy, "_central_attempt_headers", lambda h, _v, _w: h)

    lim = _StubLimiter()
    request = cast(Any, type("R", (dict,), {"query_string": ""})())
    attempt = _attempt()

    with pytest.raises(proxy._RetryAfterArmed):
        await proxy._forward_with_retry(
            request,
            {},
            None,
            "v1/messages",
            "direct",
            "http://up.example/v1/messages",
            proxy.aiohttp.ClientTimeout(total=1),
            attempt,
            "ffff6666",
            cast(Any, lim),
        )
