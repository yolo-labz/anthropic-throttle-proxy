"""pi summary stop-loss (summary_breaker.py): detection, memory, wiring, wording."""

from __future__ import annotations

import json
import re

import pytest
from _free_lane_env import patch_admission
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import capability, config, proxy, summary_breaker
from anthropic_throttle_proxy.limiter import FairBearerLimiter

MODEL = "claude-opus-5-5"
SENTINEL = "conversation-sentinel-7f3a"  # body content: must never reach a log line
MARKER = "You are a context summarization assistant."
AUTH = {"Authorization": "Bearer summary-test"}

# pi 1.5.2-83a4dd90 (dist/bundle/chunks/chunk-FXLZI5YY.js) RETRYABLE_PROVIDER_ERROR_PATTERN:
# pi re-sends a failed summary whose error text matches any of these.
PI_RETRYABLE = re.compile(
    "|".join(
        [
            "overloaded", "server_busy", "servers are currently busy",
            "currently experiencing high demand", "model is at capacity", "rate.?limit",
            "too many requests", "(?<![a-z0-9])(?:429|500|502|503|504|520|524)(?![a-z0-9])",
            "service.?(?:is.?)?(?:temporarily.?)?unavailable", "server.?error", "internal.?error",
            "provider.?returned.?(?:an.?)?error",
            "exceeded request buffer limit while retrying upstream", "unable to forward upstream",
            "upstream.?request.?failed", "upstream_stream_read_error", "stream was interrupted",
            "http/?2 stream failed", "network.?error", "connection.?error", "connection.?refused",
            "connection.?lost", "other side closed", "fetch failed",
            r"request to \S+ failed, reason:",
            "getaddrinfo", "ENOTFOUND", "EAI_AGAIN", "upstream.?connect", "reset before headers",
            "socket hang up", "socket connection was closed", "timed? out", "timeout", "terminated",
            "websocket.?closed", "websocket.?error", "ended without",
            "stream ended before message_stop", "stream ended before a terminal response event",
            "http2 request did not get a response", "stream disconnected before completion",
            "cut short after the reply had begun", "pending stream has been canceled",
            "retry delay", "you can retry your request", "try your request again",
            "please retry your request", "ResourceExhausted",
            "subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable",
        ]
    ),
    re.IGNORECASE,
)  # fmt: skip
PI_OVERFLOW = re.compile(
    r"prompt (?:is )?too long|request_too_large|exceeds the context window"
    r"|context window exceeded|maximum context length",
    re.IGNORECASE,
)


def _summary_body(*, stream: bool = False, system_as_str: bool = False) -> bytes:
    prompt = f"{MARKER} Your task is to read a conversation between a user and an AI assistant."
    system: object = prompt
    if not system_as_str:
        system = [
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": prompt},
        ]
    return json.dumps(
        {
            "model": MODEL,
            "stream": stream,
            "max_tokens": 32000,
            "system": system,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": f"<c>{SENTINEL}</c>"}]}
            ],
        }
    ).encode()


def _work_body() -> bytes:
    """An ordinary turn whose conversation merely quotes the marker."""
    return json.dumps(
        {
            "model": MODEL,
            "max_tokens": 1024,
            "system": [
                {"type": "text", "text": "You are pi.", "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": f"grep found: {MARKER} {SENTINEL}"}],
        }
    ).encode()


def _count(action: str) -> float:
    sample = proxy.REGISTRY.get_sample_value("throttle_summary_breaker_total", {"action": action})
    return sample or 0.0


@pytest.fixture(autouse=True)
def _fresh_marks(monkeypatch):
    summary_breaker.reset_for_tests()
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MODE", "enforce")
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MIN_S", 280.0)
    monkeypatch.setattr(config, "SUMMARY_BREAKER_TTL_S", 1800.0)
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MAX_SESSIONS", 4096)
    yield
    summary_breaker.reset_for_tests()


@pytest.fixture
def lines(monkeypatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(summary_breaker, "log", captured.append)
    monkeypatch.setattr(proxy, "log", captured.append)
    return captured


# --- detection -------------------------------------------------------------


def test_recognises_pi_summaries_only() -> None:
    assert summary_breaker.is_pi_summary(_summary_body())
    assert summary_breaker.is_pi_summary(_summary_body(system_as_str=True))
    assert not summary_breaker.is_pi_summary(_work_body())
    assert not summary_breaker.is_pi_summary(f"not json {MARKER}".encode())
    assert not summary_breaker.is_pi_summary(json.dumps([MARKER]).encode())
    assert not summary_breaker.is_pi_summary(json.dumps({"system": [MARKER, 3]}).encode())
    assert not summary_breaker.is_pi_summary(b"")
    assert not summary_breaker.is_pi_summary(None)


# --- memory: TTL and bound ---------------------------------------------------


def test_marks_expire_after_the_ttl(monkeypatch) -> None:
    monkeypatch.setattr(config, "SUMMARY_BREAKER_TTL_S", 60.0)
    summary_breaker._arm("old", 290.0, now=1000.0)
    summary_breaker._arm("new", 291.0, now=1050.0)
    assert summary_breaker._live_entry("old", now=1059.0) == (1000.0, 290.0)
    assert summary_breaker._live_entry("old", now=1060.0) is None  # expired and dropped
    summary_breaker._arm("third", 292.0, now=1111.0)  # arming prunes the expired front
    assert list(summary_breaker._armed) == ["third"]


def test_marks_are_bounded_oldest_first(monkeypatch) -> None:
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MAX_SESSIONS", 3)
    for i, key in enumerate("abcd"):
        summary_breaker._arm(key, 290.0, now=100.0 + i)
    summary_breaker._arm("b", 290.0, now=110.0)  # re-arm refreshes b
    summary_breaker._arm("e", 290.0, now=111.0)
    assert list(summary_breaker._armed) == ["d", "b", "e"]
    assert summary_breaker.armed_sessions() == 3


# --- settle: what arms and what clears ---------------------------------------


def _tagged() -> dict:
    return {summary_breaker.session_key: "k" * 16}


def test_only_a_late_client_cut_arms_and_success_clears(lines) -> None:
    request = _tagged()
    summary_breaker.settle(request, 499, 279.0)  # cut early: may be queue or Esc
    summary_breaker.settle(request, 529, 300.0)  # upstream throttle, not a client cut
    summary_breaker.settle({}, 499, 300.0)  # untagged: not a pi summary
    assert summary_breaker.armed_sessions() == 0
    summary_breaker.settle(request, 499, 291.0)
    assert summary_breaker.armed_sessions() == 1
    summary_breaker.settle(request, 200, 120.0)
    assert summary_breaker.armed_sessions() == 0
    assert any("armed" in line for line in lines) and any("cleared" in line for line in lines)


# --- wording ---------------------------------------------------------------


@pytest.mark.parametrize("remaining_s", [1.0, 59.0, 1799.0, 21600.0])
def test_refusal_is_not_retryable_or_overflow_for_pi(remaining_s: float) -> None:
    response = summary_breaker._refusal(remaining_s)
    payload = json.loads(response.body)
    assert response.status == 400
    assert payload["error"]["type"] == "invalid_request_error"
    seen_by_pi = f"400 {json.dumps(payload)}"  # pi's errorMessage for an HTTP error
    assert not PI_RETRYABLE.search(seen_by_pi), PI_RETRYABLE.search(seen_by_pi)
    assert not PI_OVERFLOW.search(seen_by_pi)


# --- the real handler --------------------------------------------------------


@pytest.fixture
async def gateway(monkeypatch, proxy_admission_app):
    """Real admission handler; upstream forwarding replaced by a scripted outcome."""
    patch_admission(
        monkeypatch,
        UPSTREAM="http://127.0.0.1:9",
        CENTRAL_URL="",
        QUEUE_MODE="fair",
        MAX_CONCURRENT=4,
        AIMD_INITIAL_CONCURRENT=4,
        PRIORITY_RESERVE_SLOTS=0,
        RATE_PUSHBACK_RETRIES=0,
        MIN_DISPATCH_GAP_S=0,
        QUEUE_MAX_WAIT_S=3,
    )
    forwarded: list[int] = []
    script = {"status": 499}

    async def fake_forward(request, headers, body, path, via, url, _timeout, attempt, *_a, **_k):
        forwarded.append(script["status"])
        attempt.final_status = script["status"]
        if script["status"] == 499:
            return web.Response(status=499)
        return web.json_response({"type": "message", "content": []}, status=script["status"])

    async def no_shrink(_self):  # pragma: no cover - must not run
        raise AssertionError("AIMD shrink reached")

    async def no_hold(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("keepalive hold reached")

    monkeypatch.setattr(proxy, "_forward_with_retry", fake_forward)
    monkeypatch.setattr(FairBearerLimiter, "shrink", no_shrink)
    monkeypatch.setattr(proxy, "_hold_deferral", no_hold)
    monkeypatch.setattr(proxy, "_keepalive_hold_and_retry", no_hold)
    async with TestClient(TestServer(proxy_admission_app)) as client:
        client.forwarded = forwarded
        client.script = script
        yield client


async def _post(client: TestClient, body: bytes) -> tuple[int, dict, bytes]:
    async with client.post("/v1/messages", data=body, headers=AUTH) as response:
        return response.status, dict(response.headers), await response.read()


async def test_cut_summary_arms_then_refuses_before_any_upstream_work(
    gateway, monkeypatch, lines
) -> None:
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MIN_S", 0.0)  # the fake cut is instant
    refused_before = _count("refused")
    retries_before = config.state["upstream_retries"]
    status, _, _ = await _post(gateway, _summary_body())
    assert status == 499 and gateway.forwarded == [499]
    assert summary_breaker.armed_sessions() == 1

    for stream in (False, True):  # a streaming summary is refused, never held
        status, headers, raw = await _post(gateway, _summary_body(stream=stream))
        assert status == 400
        assert headers[config.SUMMARY_BREAKER_HEADER] == "1"
        assert headers[config.MARKER_HEADER] == "1"  # gateway-generated
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(raw)["error"]["type"] == "invalid_request_error"
    assert gateway.forwarded == [499]  # nothing reached upstream
    assert _count("refused") == refused_before + 2
    assert config.state["upstream_retries"] == retries_before  # no pushback retry

    gateway.script["status"] = 200
    status, _, _ = await _post(gateway, _work_body())  # ordinary turns always pass
    assert status == 200 and gateway.forwarded == [499, 200]
    assert not [line for line in lines if SENTINEL in line or MARKER in line]


async def test_observe_counts_but_passes_and_a_completed_summary_clears(
    gateway, monkeypatch
) -> None:
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MIN_S", 0.0)
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MODE", "observe")
    await _post(gateway, _summary_body())
    assert summary_breaker.armed_sessions() == 1
    observed_before = _count("observed")
    gateway.script["status"] = 200
    status, headers, _ = await _post(gateway, _summary_body())
    assert status == 200 and config.SUMMARY_BREAKER_HEADER not in headers
    assert _count("observed") == observed_before + 1
    assert summary_breaker.armed_sessions() == 0  # the completed summary cleared it


async def test_off_neither_tags_nor_refuses(gateway, monkeypatch) -> None:
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MIN_S", 0.0)
    monkeypatch.setattr(config, "SUMMARY_BREAKER_MODE", "off")
    for _ in range(2):
        status, _, _ = await _post(gateway, _summary_body())
        assert status == 499
    assert gateway.forwarded == [499, 499]
    assert summary_breaker.armed_sessions() == 0


def test_config_fingerprint_still_canonical() -> None:
    """No uncanonicalisable module-level name leaked into proxy (fails closed if so)."""
    digest = capability.config_sha256(config, proxy)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
