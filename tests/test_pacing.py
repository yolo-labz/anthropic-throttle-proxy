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


# The exact unified headers Anthropic returned for account A during the
# 03/07/2026 concurrency-concentration incident: budget fully `allowed` at
# 19%/23% while the account was 429ing purely on per-account concurrency.
_ALLOWED_LOW = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.19",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.23",
}


def test_budget_under_pressure_classifies_unified_headers():
    # Concurrency 429: every window allowed + low util → NOT budget.
    assert proxy._budget_under_pressure(_ALLOWED_LOW) is False
    # Budget soft-throttle: a warning/rejected status on any window → budget.
    assert (
        proxy._budget_under_pressure(
            {**_ALLOWED_LOW, "anthropic-ratelimit-unified-status": "rejected"}
        )
        is True
    )
    assert (
        proxy._budget_under_pressure(
            {**_ALLOWED_LOW, "anthropic-ratelimit-unified-5h-status": "allowed_warning"}
        )
        is True
    )
    # Utilization at/over the warn line → budget even while status still "allowed".
    hot = {**_ALLOWED_LOW, "anthropic-ratelimit-unified-5h-utilization": "0.95"}
    assert proxy._budget_under_pressure(hot) is True
    # Partial/malformed unified headers cannot prove a concurrency-only 429.
    assert proxy._budget_under_pressure({"anthropic-ratelimit-unified-status": "allowed"}) is True
    # No unified headers (API-key traffic) → conservative: assume budget.
    assert proxy._budget_under_pressure({}) is True
    assert proxy._budget_under_pressure(None) is True


def test_budget_under_pressure_falls_back_to_cached_unified():
    # A concurrency/rate 429 frequently arrives with NO unified headers on its
    # own response. Without a bid we stay conservative (assume budget); with a
    # bid we consult the bearer's FRESH cached unified state (05/07/2026: the
    # fresh account, u7d=0.09/status=allowed, whose headerless 429s were
    # collapsing to the 30s budget backoff and starving throughput).
    bid = "cachetest0"
    now = time.time()
    fresh = proxy._parse_unified(_ALLOWED_LOW)
    proxy.bearer_state.pop(bid, None)
    try:
        # No cache yet → headerless 429 stays conservative (budget), even with bid.
        assert proxy._budget_under_pressure(None, bid) is True
        assert proxy._budget_under_pressure({}, bid) is True
        # Fresh, recently-allowed low-util cache → headerless 429 classifies as
        # concurrency, NOT budget.
        proxy.bearer_state[bid] = {"unified": fresh, "unified_at": now}
        assert proxy._budget_under_pressure(None, bid) is False
        assert proxy._budget_under_pressure({}, bid) is False
        # STALE cache (older than the freshness window) must NOT be trusted: a
        # budget wall stops refreshing it, so an aged "allowed" sample falls back
        # to the conservative budget default.
        proxy.bearer_state[bid] = {
            "unified": fresh,
            "unified_at": now - proxy.UNIFIED_CACHE_FRESH_S - 5,
        }
        assert proxy._budget_under_pressure(None, bid) is True
        # A cache with no timestamp at all is also untrusted.
        proxy.bearer_state[bid] = {"unified": fresh}
        assert proxy._budget_under_pressure(None, bid) is True
        # Headers on THIS response always win over the cache: a rejected window
        # is budget even if a fresh cache still says allowed.
        proxy.bearer_state[bid] = {"unified": fresh, "unified_at": now}
        assert (
            proxy._budget_under_pressure(
                {**_ALLOWED_LOW, "anthropic-ratelimit-unified-status": "rejected"}, bid
            )
            is True
        )
        # Fresh cache showing a real wall → headerless 429 correctly reads budget.
        proxy.bearer_state[bid] = {
            "unified": proxy._parse_unified(
                {**_ALLOWED_LOW, "anthropic-ratelimit-unified-7d-status": "rejected"}
            ),
            "unified_at": now,
        }
        assert proxy._budget_under_pressure(None, bid) is True
    finally:
        proxy.bearer_state.pop(bid, None)
    # An unknown bid must never KeyError — just stay conservative.
    assert proxy._budget_under_pressure(None, "never-seen") is True


def test_pushback_pause_short_cooldown_for_concurrency_429():
    from anthropic_throttle_proxy import config

    # Real Retry-After always wins, verbatim, never synthetic.
    assert proxy._pushback_pause({"retry-after": "171"}) == (171.0, False)
    # Concurrency 429 (budget allowed, low util) → short cooldown, not 30s.
    pause, synthetic = proxy._pushback_pause(_ALLOWED_LOW)
    assert pause == config.CONCURRENCY_COOLDOWN_S
    assert synthetic is True
    assert pause < config.AIMD_BACKOFF_S  # the whole point: NOT the budget backoff
    # Budget soft-throttle (rejected) → full AIMD cooldown.
    rejected = {**_ALLOWED_LOW, "anthropic-ratelimit-unified-status": "rejected"}
    assert proxy._pushback_pause(rejected) == (config.AIMD_BACKOFF_S, True)
    # No headers at all → conservative full backoff (unchanged legacy behavior).
    assert proxy._pushback_pause({}) == (config.AIMD_BACKOFF_S, True)


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
