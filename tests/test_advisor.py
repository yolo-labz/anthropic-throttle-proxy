"""Tests for the GROQ advisor + the proxy's out-of-band auto-advisor.

No network: the GROQ HTTP call is faked by monkeypatching
``advisor_impl.aiohttp.ClientSession``. The auto-advisor tests monkeypatch
``advisor_impl.recommend`` (picked up by `_maybe_advise`'s lazy import).
"""

import asyncio

import pytest

from anthropic_throttle_proxy import proxy
from anthropic_throttle_proxy.ui import advisor_impl


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def post(self, *_, **__):
        return _FakeResp(self._payload, self._status)


def _patch_session(monkeypatch, payload, status=200):
    monkeypatch.setattr(
        advisor_impl.aiohttp,
        "ClientSession",
        lambda *a, **k: _FakeSession(payload, status),
    )


async def test_recommend_requires_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        await advisor_impl.recommend({"inflight": 0})


async def test_recommend_parses_groq_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    payload = {"choices": [{"message": {"content": "lower CLAUDE_API_THROTTLE_MAX = 4"}}]}
    _patch_session(monkeypatch, payload)
    out = await advisor_impl.recommend({"retries": 12, "inflight": 1})
    assert "CLAUDE_API_THROTTLE_MAX" in out


async def test_recommend_handles_bad_shape(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    _patch_session(monkeypatch, {"unexpected": True})
    out = await advisor_impl.recommend({})
    assert "unexpected response shape" in out


async def test_maybe_advise_sets_state_and_debounces(monkeypatch):
    calls = []

    async def fake_recommend(snapshot):
        calls.append(snapshot)
        return "diagnosis text"

    monkeypatch.setattr(advisor_impl, "recommend", fake_recommend)
    monkeypatch.setattr(proxy, "ADVISOR_ENABLED", True)
    monkeypatch.setattr(proxy, "ADVISOR_DEBOUNCE_S", 120.0)
    monkeypatch.setattr(proxy, "_last_advice_ts", 0.0)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    proxy.state["last_advisor"] = None

    await proxy._maybe_advise("beef1234", 429)
    assert proxy.state["last_advisor"]["text"] == "diagnosis text"
    assert proxy.state["last_advisor"]["trigger"] == "status=429 bid=beef1234"
    assert len(calls) == 1

    # Second call inside the debounce window is skipped.
    await proxy._maybe_advise("beef1234", 429)
    assert len(calls) == 1


async def test_maybe_advise_swallows_errors(monkeypatch):
    async def boom(_snapshot):
        raise RuntimeError("groq down")

    monkeypatch.setattr(advisor_impl, "recommend", boom)
    monkeypatch.setattr(proxy, "ADVISOR_ENABLED", True)
    monkeypatch.setattr(proxy, "ADVISOR_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(proxy, "_last_advice_ts", 0.0)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    proxy.state["last_advisor"] = None

    # Must not raise — failures are stored, not propagated.
    await proxy._maybe_advise("beef1234", 529)
    assert "advisor error" in proxy.state["last_advisor"]["text"]


async def test_maybe_advise_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(proxy, "ADVISOR_ENABLED", False)
    proxy.state["last_advisor"] = None
    await proxy._maybe_advise("beef1234", 429)
    assert proxy.state["last_advisor"] is None


async def test_schedule_advisor_still_runs_for_message_throttles(monkeypatch):
    calls: list[tuple[str, int]] = []

    async def fake_advise(trigger_bid: str, trigger_status: int) -> None:
        calls.append((trigger_bid, trigger_status))

    monkeypatch.setattr(proxy, "ADVISOR_ENABLED", True)
    monkeypatch.setattr(proxy, "_maybe_advise", fake_advise)

    proxy._schedule_advisor("beef1234", 429, "v1/messages")
    await asyncio.sleep(0)

    assert calls == [("beef1234", 429)]


def test_advisor_snapshot_includes_trigger():
    snap = proxy._advisor_snapshot("beef1234", 429)
    assert snap["trigger"] == {"bearer": "beef1234", "status": 429}
    assert "bearers" in snap and "max_concurrent" in snap
