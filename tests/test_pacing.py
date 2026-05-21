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


def test_529_is_split_from_rate_statuses():
    assert 529 not in proxy.AIMD_STATUSES
    assert 529 in proxy.OVERLOAD_STATUSES
    assert 429 in proxy.AIMD_STATUSES
    # Advisor still fires on all throttle signals.
    assert proxy.THROTTLE_STATUSES == {429, 503, 529}


async def test_shrink_uses_cubic_decrease_and_floor():
    lim = FairBearerLimiter(32, "fair")
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
    t0 = time.time()
    await lim.wait_retry_after()
    assert time.time() - t0 >= 0.04  # actually waited out the window
    # Window closed → no-op, returns promptly.
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
