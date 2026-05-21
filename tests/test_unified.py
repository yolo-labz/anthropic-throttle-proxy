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
    await proxy._apply_unified("bid1", {}, lim, _oauth_meta(util_5h="0.9"))
    assert lim.max_concurrent == 22  # shrank one CUBIC step (32*0.7)


async def test_apply_unified_no_shrink_below_target(monkeypatch):
    monkeypatch.setattr(proxy, "UTILIZATION_TARGET", 0.95)
    lim = FairBearerLimiter(32, "fair")
    await proxy._apply_unified("bid1", {}, lim, _oauth_meta(util_5h="0.87"))
    assert lim.max_concurrent == 32  # below target → surface only


async def test_apply_unified_target_off_is_observe_only():
    # UTILIZATION_TARGET defaults to 0 → never shrinks proactively, even at 0.99.
    assert proxy.UTILIZATION_TARGET == 0
    lim = FairBearerLimiter(32, "fair")
    bstate = {}
    await proxy._apply_unified("bid1", bstate, lim, _oauth_meta(util_5h="0.99"))
    assert lim.max_concurrent == 32
    assert bstate["unified"]["util_5h"] == 0.99  # but still surfaced
