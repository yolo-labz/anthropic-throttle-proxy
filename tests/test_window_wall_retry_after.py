"""A saturated window must quote its reset, not the queue's drain estimate.

Regression for the 26/10/2026 12:16 retry storm: 5h util 0.91 / 7d 0.90 with
live slots collapsing to 1 made every request 503 with a drain-estimate
Retry-After (seconds), so pi — which ignores Retry-After and retries 10x at a
fixed 6 s — hammered a wall that could not move for 93 minutes.
"""

import time

from anthropic_throttle_proxy import proxy


def _state(monkeypatch, unified):
    monkeypatch.setitem(proxy.bearer_state, "bid", {"unified": unified})


def test_at_the_wall_quotes_the_window_reset(monkeypatch):
    reset = int(time.time()) + 5600  # 93 min, the measured case
    _state(
        monkeypatch,
        {
            "util_5h": 0.91,
            "reset_5h": reset,
            "util_7d": 0.90,
            "reset_7d": reset + 86400,
            "representative_claim": "five_hour",
        },
    )
    got = proxy._window_wall_retry_after("bid")
    assert got is not None
    assert abs(got - 5600) <= 2


def test_below_the_target_keeps_the_drain_estimate(monkeypatch):
    reset = int(time.time()) + 5600
    _state(
        monkeypatch,
        {
            "util_5h": 0.40,
            "reset_5h": reset,
            "util_7d": 0.40,
            "reset_7d": reset,
            "representative_claim": "five_hour",
        },
    )
    assert proxy._window_wall_retry_after("bid") is None


def test_unknown_bearer_is_none_not_an_exception(monkeypatch):
    monkeypatch.setitem(proxy.bearer_state, "bid", {})
    assert proxy._window_wall_retry_after("bid") is None


def test_missing_reset_is_none(monkeypatch):
    _state(monkeypatch, {"util_5h": 0.95, "representative_claim": "five_hour"})
    assert proxy._window_wall_retry_after("bid") is None


def test_expired_reset_is_none_never_zero(monkeypatch):
    """A passed reset means the window rolled over; a 0 Retry-After would invite
    an immediate retry against state we have not refreshed yet."""
    _state(
        monkeypatch,
        {"util_5h": 0.99, "reset_5h": int(time.time()) - 30, "representative_claim": "five_hour"},
    )
    assert proxy._window_wall_retry_after("bid") is None


def test_broken_meter_never_blocks_the_refusal_path(monkeypatch):
    monkeypatch.setitem(proxy.bearer_state, "bid", {"unified": "not-a-dict"})
    assert proxy._window_wall_retry_after("bid") is None
