"""Tests for WS-B2 OAuth unified-window utilization handling.

Header shapes mirror what a real Claude Code Max-20x token returns (measured
21/05/2026): anthropic-ratelimit-unified-* with 5h/7d utilization fractions,
status, and epoch reset — NOT the API-key remaining-count family.
"""

import time

from anthropic_throttle_proxy import proxy
from anthropic_throttle_proxy.proxy import FairBearerLimiter


def _oauth_meta(status="allowed", util_5h="0.87", util_7d="0.6", claim="five_hour", reset=None):
    reset = reset if reset is not None else int(time.time()) + 300
    return {
        "anthropic-ratelimit-unified-status": status,
        "anthropic-ratelimit-unified-reset": str(reset),
        "anthropic-ratelimit-unified-representative-claim": claim,
        "anthropic-ratelimit-unified-5h-status": status,
        "anthropic-ratelimit-unified-5h-utilization": util_5h,
        "anthropic-ratelimit-unified-5h-reset": str(reset),
        "anthropic-ratelimit-unified-7d-utilization": util_7d,
    }


def test_parse_unified_extracts_fields():
    u = proxy._parse_unified(_oauth_meta(reset=1779339600))
    assert u["status"] == "allowed"
    assert u["reset"] == 1779339600
    assert u["representative_claim"] == "five_hour"
    assert u["util_5h"] == 0.87
    assert u["util_7d"] == 0.6


def test_parse_unified_empty_for_api_key_traffic():
    assert proxy._parse_unified({"retry-after": "5"}) == {}
    assert proxy._parse_unified({}) == {}


def test_binding_utilization_follows_representative_claim():
    assert (
        proxy._binding_utilization(
            {"util_5h": 0.87, "util_7d": 0.6, "representative_claim": "five_hour"}
        )
        == 0.87
    )
    assert (
        proxy._binding_utilization(
            {"util_5h": 0.5, "util_7d": 0.9, "representative_claim": "seven_day"}
        )
        == 0.9
    )
    # Unknown claim → max() fallback.
    assert (
        proxy._binding_utilization({"util_5h": 0.5, "util_7d": 0.9, "representative_claim": None})
        == 0.9
    )
    assert proxy._binding_utilization({}) is None


async def test_apply_unified_pauses_when_rejected():
    lim = FairBearerLimiter(8, "fair")
    bstate = {}
    reset = int(time.time()) + 300
    await proxy._apply_unified("bid1", bstate, lim, _oauth_meta(status="rejected", reset=reset))
    assert bstate["unified"]["status"] == "rejected"
    # Proactive pause: dispatch barred until the window resets.
    assert lim._retry_after_until > time.time() + 250


async def test_apply_unified_glides_when_target_crossed(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.85)
    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = lim.hard_max
    await proxy._apply_unified("bid1", {}, lim, _oauth_meta(util_5h="0.9"))
    assert lim.max_concurrent == 22  # shrank one CUBIC step (32*0.7)


async def test_apply_unified_glides_once_per_reset_window(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.85)
    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = lim.hard_max
    bstate = {}
    reset = int(time.time()) + 300

    await proxy._apply_unified("bid1", bstate, lim, _oauth_meta(util_5h="0.9", reset=reset))
    assert lim.max_concurrent == 22

    await proxy._apply_unified("bid1", bstate, lim, _oauth_meta(util_5h="0.91", reset=reset))
    assert lim.max_concurrent == 22


async def test_apply_unified_no_shrink_below_target(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.95)
    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = lim.hard_max
    await proxy._apply_unified("bid1", {}, lim, _oauth_meta(util_5h="0.87"))
    assert lim.max_concurrent == 32  # below target → surface only


async def test_apply_unified_target_off_is_observe_only():
    # UTILIZATION_TARGET defaults to 0 → never shrinks proactively, even at 0.99.
    assert proxy.UTILIZATION_TARGET == 0
    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = lim.hard_max
    bstate = {}
    await proxy._apply_unified("bid1", bstate, lim, _oauth_meta(util_5h="0.99"))
    assert lim.max_concurrent == 32
    assert bstate["unified"]["util_5h"] == 0.99  # but still surfaced


# --- WS-B2 early-warning (PR #49): warn-only signal before "rejected" ---------


def _warn_count(bid: str, window: str) -> float:
    """Current value of the unified-warning counter for one (bearer, window)."""
    val = proxy.REGISTRY.get_sample_value(
        "anthropic_ratelimit_unified_warnings_total",
        {"bearer": bid, "window": window},
    )
    return val or 0.0


def test_binding_window_mirrors_binding_utilization():
    # representative_claim wins.
    assert (
        proxy._binding_window(
            {"util_5h": 0.87, "util_7d": 0.6, "representative_claim": "five_hour"}
        )
        == "5h"
    )
    assert (
        proxy._binding_window({"util_5h": 0.5, "util_7d": 0.9, "representative_claim": "seven_day"})
        == "7d"
    )
    # Unknown claim → the window max() would pick (7d only when strictly greater).
    assert proxy._binding_window({"util_5h": 0.5, "util_7d": 0.9}) == "7d"
    assert proxy._binding_window({"util_5h": 0.9, "util_7d": 0.5}) == "5h"
    # Single window present, or none.
    assert proxy._binding_window({"util_7d": 0.4}) == "7d"
    assert proxy._binding_window({}) is None


def test_utilization_warn_default_on():
    # The warn-only early signal ships ON by default (pure observability, never
    # shrinks — distinct from the default-off UTILIZATION_TARGET brake).
    assert proxy.UTILIZATION_WARN == 0.9


async def test_apply_unified_warns_without_shrinking(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    assert proxy.UTILIZATION_TARGET == 0  # brake off → warn must not shrink
    lim = FairBearerLimiter(32, "fair")
    lim.max_concurrent = lim.hard_max
    bid = "warn-bid-shrink"
    before = _warn_count(bid, "5h")
    await proxy._apply_unified(bid, {}, lim, _oauth_meta(util_5h="0.95"))
    assert _warn_count(bid, "5h") == before + 1  # one warning emitted
    assert lim.max_concurrent == 32  # ceiling untouched (warn-only)


async def test_apply_unified_warns_once_per_reset_window(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    lim = FairBearerLimiter(32, "fair")
    bid = "warn-bid-once"
    bstate = {}
    reset = int(time.time()) + 300
    before = _warn_count(bid, "5h")
    await proxy._apply_unified(bid, bstate, lim, _oauth_meta(util_5h="0.95", reset=reset))
    await proxy._apply_unified(bid, bstate, lim, _oauth_meta(util_5h="0.97", reset=reset))
    assert _warn_count(bid, "5h") == before + 1  # debounced within the window


async def test_apply_unified_warns_again_on_new_window(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    lim = FairBearerLimiter(32, "fair")
    bid = "warn-bid-newwin"
    bstate = {}
    before = _warn_count(bid, "5h")
    await proxy._apply_unified(
        bid, bstate, lim, _oauth_meta(util_5h="0.95", reset=int(time.time()) + 300)
    )
    await proxy._apply_unified(
        bid, bstate, lim, _oauth_meta(util_5h="0.95", reset=int(time.time()) + 9000)
    )
    assert _warn_count(bid, "5h") == before + 2  # new reset epoch → warns again


async def test_apply_unified_no_warn_below_threshold(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    lim = FairBearerLimiter(32, "fair")
    bid = "warn-bid-below"
    before = _warn_count(bid, "5h")
    await proxy._apply_unified(bid, {}, lim, _oauth_meta(util_5h="0.87"))  # < 0.9
    assert _warn_count(bid, "5h") == before


async def test_apply_unified_warn_disabled(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.0)
    lim = FairBearerLimiter(32, "fair")
    bid = "warn-bid-off"
    before = _warn_count(bid, "5h")
    await proxy._apply_unified(bid, {}, lim, _oauth_meta(util_5h="0.99"))
    assert _warn_count(bid, "5h") == before  # disabled → silent even at 0.99


async def test_apply_unified_rejected_skips_warn(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_WARN", 0.9)
    lim = FairBearerLimiter(8, "fair")
    bid = "warn-bid-rejected"
    reset = int(time.time()) + 300
    before = _warn_count(bid, "5h")
    await proxy._apply_unified(
        bid, {}, lim, _oauth_meta(status="rejected", util_5h="0.99", reset=reset)
    )
    # rejected → proactive pause; the warn path is short-circuited before it.
    assert lim._retry_after_until > time.time() + 250
    assert _warn_count(bid, "5h") == before
