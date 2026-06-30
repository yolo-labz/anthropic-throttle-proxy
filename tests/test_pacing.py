"""Tests for WS-B1 header-aware pacing: rate-limit header extraction,
retry-after honoring, 529 split, and the CUBIC-style AIMD decrease.
"""

import time

from multidict import CIMultiDict

from anthropic_throttle_proxy import proxy
from anthropic_throttle_proxy.proxy import FairBearerLimiter


def test_extract_ratelimit_is_case_insensitive_and_sparse():
    headers = CIMultiDict(
        {
            "Retry-After": "7",
            "Anthropic-RateLimit-Requests-Remaining": "42",
            "content-type": "application/json",  # not a rate-limit header
        }
    )
    meta = proxy._extract_ratelimit(headers)
    assert meta["retry-after"] == "7"
    assert meta["anthropic-ratelimit-requests-remaining"] == "42"
    # Only present rate-limit keys are returned; content-type is dropped.
    assert "content-type" not in meta
    assert "anthropic-ratelimit-tokens-remaining" not in meta


def test_extract_ratelimit_empty_when_absent():
    assert proxy._extract_ratelimit(CIMultiDict({"x-foo": "bar"})) == {}


def test_parse_retry_after():
    assert proxy._parse_retry_after({"retry-after": "171"}) == 171.0
    assert proxy._parse_retry_after({"retry-after": "0"}) == 0.0
    assert proxy._parse_retry_after({}) == 0.0
    assert proxy._parse_retry_after(None) == 0.0
    assert proxy._parse_retry_after({"retry-after": "not-a-number"}) == 0.0


def test_extract_zai_quota_body_generates_retry_after():
    body = b'{"error":{"code":"1316","message":"quota exceeded","reset_time":1700000100}}'
    meta = proxy._extract_zai_ratelimit_from_body(body, now=1700000000, quota_jitter_s=7)

    assert meta["zai-error-code"] == "1316"
    assert meta["zai-quota-gate"] == "true"
    assert meta["zai-reset-epoch"] == "1700000100"
    assert meta["zai-resume-epoch"] == "1700000107"
    assert meta["retry-after"] == "107"
    assert proxy._is_zai_quota_gate(meta) is True


def test_extract_zai_message_reset_assumes_beijing_time():
    body = (
        b'{"error":{"code":"1308","message":'
        b'"Usage limit reached for 5 hour. Your limit will reset at 2026-06-30 12:40:46"}}'
    )
    meta = proxy._extract_zai_ratelimit_from_body(body, now=0, quota_jitter_s=0)

    assert meta["zai-quota-gate"] == "true"
    assert meta["zai-reset-epoch"] == "1782794446"
    assert meta["retry-after"] == "1782794446"


def test_extract_zai_concurrency_body_is_not_quota_gate():
    body = b'{"error":{"code":"1302","message":"too many concurrent requests"}}'
    meta = proxy._extract_zai_ratelimit_from_body(body, now=1700000000, quota_jitter_s=7)

    assert meta == {"zai-error-code": "1302"}
    assert proxy._is_zai_quota_gate(meta) is False


def test_529_is_split_from_rate_statuses():
    assert 529 not in proxy.AIMD_STATUSES
    assert 529 in proxy.OVERLOAD_STATUSES
    assert 429 in proxy.AIMD_STATUSES
    # Advisor still fires on all throttle signals.
    assert proxy.THROTTLE_STATUSES == {429, 503, 529}


def test_aimd_min_is_clamped_to_floor_one():
    # Constitution III: floor must stay >=1 so traffic never fully blocks.
    # The clamp lives at config import: AIMD_MIN = max(1, int(env)).
    # Verify both the clamp expression and the live module value.
    from anthropic_throttle_proxy import config

    assert config.AIMD_MIN >= 1  # current module satisfies invariant
    # Clamp expression must coerce 0 / negative / valid values to >=1.
    assert max(1, int("0")) == 1
    assert max(1, int("-5")) == 1
    assert max(1, int("3")) == 3
    # Source-level guard: the assignment uses max(1, ...) so the floor cannot
    # be subverted by an operator setting THROTTLE_AIMD_MIN=0.
    import inspect

    src = inspect.getsource(config)
    assert 'AIMD_MIN = max(1, int(os.environ.get("THROTTLE_AIMD_MIN"' in src


async def test_shrink_uses_cubic_decrease_and_floor():
    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = lim.hard_max
    assert await lim.shrink() == 22  # int(32 * 0.7)
    assert await lim.shrink() == 15  # int(22 * 0.7)
    lim.max_concurrent = 2
    assert await lim.shrink() == 1  # min(int(2*0.7)=1, 1) -> 1
    lim.max_concurrent = 1
    assert await lim.shrink() == proxy.AIMD_MIN  # floored, never below AIMD_MIN


async def test_note_and_wait_retry_after():
    lim = FairBearerLimiter(8, "fair")
    until = lim.note_retry_after(0.05)
    assert until > time.time()
    assert lim.retry_after_remaining() > 0
    t0 = time.time()
    await lim.wait_retry_after()
    assert time.time() - t0 >= 0.04  # actually waited out the window
    # Window closed → no-op, returns promptly.
    assert lim.retry_after_remaining() == 0
    t1 = time.time()
    await lim.wait_retry_after()
    assert time.time() - t1 < 0.02


async def test_note_retry_after_only_extends():
    lim = FairBearerLimiter(8, "fair")
    far = lim.note_retry_after(100)
    near = lim.note_retry_after(1)  # shorter — must NOT shrink the window
    assert near == far


async def test_grow_blocked_during_retry_after_window():
    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = 4
    lim._successes_since_throttle = proxy.AIMD_RAMP_AFTER
    lim._last_throttle_at = 0.0  # AIMD cooldown long elapsed
    lim._retry_after_until = time.time() + 100  # but server says wait
    assert await lim.grow() is None  # blocked by retry-after window
    lim._retry_after_until = 0.0
    lim._successes_since_throttle = proxy.AIMD_RAMP_AFTER
    assert await lim.grow() == 5  # now ramps


def test_snapshot_exposes_retry_after_until():
    lim = FairBearerLimiter(8, "fair")
    lim.note_retry_after(30)
    snap = lim.snapshot()
    assert "retry_after_until" in snap
    assert snap["retry_after_until"] > time.time()


def test_new_limiter_starts_at_initial_live_cap(monkeypatch):
    monkeypatch.setattr(proxy.config, "AIMD_INITIAL_CONCURRENT", 2)
    lim = FairBearerLimiter(8, "fair")
    assert lim.hard_max == 8
    assert lim.max_concurrent == 2


async def test_hard_cap_increase_keeps_live_cap_for_discovery(monkeypatch):
    from anthropic_throttle_proxy import limiter as limiter_mod

    monkeypatch.setattr(proxy.config, "AIMD_INITIAL_CONCURRENT", 1)
    lim = FairBearerLimiter(1, "fair")
    await limiter_mod._retune_limiter_hard_max("bid", lim, 8)  # noqa: SLF001
    assert lim.hard_max == 8
    assert lim.max_concurrent == 1


async def test_explicit_live_floor_lifts_existing_limiter(monkeypatch):
    from anthropic_throttle_proxy import limiter as limiter_mod

    monkeypatch.setattr(proxy.config, "AIMD_INITIAL_CONCURRENT", 1)
    lim = FairBearerLimiter(12, "fair")
    assert lim.max_concurrent == 1

    await limiter_mod._retune_limiter_hard_max("bid", lim, 12, live_floor=6)  # noqa: SLF001
    assert lim.hard_max == 12
    assert lim.max_concurrent == 6


async def test_clean_successes_grow_from_initial_cap(monkeypatch):
    monkeypatch.setattr(proxy.config, "AIMD_INITIAL_CONCURRENT", 1)
    monkeypatch.setattr(proxy.config, "AIMD_RAMP_AFTER", 2)
    monkeypatch.setattr(proxy.config, "AIMD_BACKOFF_S", 0)
    lim = FairBearerLimiter(4, "fair")
    assert await lim.grow() is None
    assert await lim.grow() == 2
    assert lim.max_concurrent == 2
