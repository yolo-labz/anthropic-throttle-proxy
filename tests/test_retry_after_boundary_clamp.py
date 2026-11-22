"""A 7d-rejected Retry-After is re-probed every reject cap, not latched for a week.

13/11/2026 18:00:00 BRT: a request dispatched across the weekly reset got 429 with
``Retry-After: 604799`` and 7d ``rejected`` (status for the old week). The same
response took two paths: ``_maybe_pause_rejected`` capped its pause at
``RETRY_AFTER_REJECT_CAP_S`` (900 s) but ``_pushback_pause`` returned the real value
uncapped, and ``note_retry_after`` only extends, so gmail sat latched for 2 h 40 min
at 0 % use (cleared only by the 20:39 restart's restore cap).
"""

from __future__ import annotations

import time as _real_time

import pytest

from anthropic_throttle_proxy import config, limiter, proxy
from anthropic_throttle_proxy.ratelimit import _parse_unified

CAP = 900.0
PFX = "anthropic-ratelimit-unified-"


class _Clock:
    """Fake wall clock for the two modules that judge a Retry-After window."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str):
        return getattr(_real_time, name)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock(_real_time.time())
    monkeypatch.setattr(limiter, "time", c)
    monkeypatch.setattr(proxy, "time", c)
    monkeypatch.setattr(config, "RETRY_AFTER_REJECT_CAP_S", CAP)
    monkeypatch.setattr(config, "MAX_HOLD_RETRY_AFTER_S", 240.0)
    return c


def _meta(retry_after: float, *, status_7d: str = "rejected", reset_in: float = 604799.0) -> dict:
    now = int(_real_time.time())
    return {
        "retry-after": str(int(retry_after)),
        PFX + "status": status_7d,
        PFX + "7d-status": status_7d,
        PFX + "7d-utilization": "1.0" if status_7d == "rejected" else "0.5",
        PFX + "7d-reset": str(now + int(reset_in)),
        PFX + "reset": str(now + int(reset_in)),
        PFX + "5h-status": "allowed",
        PFX + "5h-utilization": "0.1",
    }


def _lim(bid: str) -> limiter.FairBearerLimiter:
    return limiter.FairBearerLimiter(8, "fair", bearer_id=bid)


def test_boundary_response_pauses_for_the_cap_not_the_header(clock: _Clock) -> None:
    pause, synthetic = proxy._pushback_pause(_meta(604799), "gmail")
    assert (pause, synthetic) == (CAP, False)  # still a REAL pause: AIMD semantics unchanged


def test_the_two_pause_paths_agree_on_the_same_response(clock: _Clock) -> None:
    """(b) ``_maybe_pause_rejected`` and ``_pushback_pause`` latch the same window."""
    meta = _meta(604799)
    via_rejected, via_pushback = _lim("bid-rej"), _lim("bid-push")
    assert proxy._maybe_pause_rejected("bid-rej", via_rejected, _parse_unified(meta), meta=meta)
    proxy._note_retry_after_if_set(via_pushback, meta, "bid-push")
    assert via_rejected.retry_after_remaining() == pytest.approx(CAP, abs=1)
    assert via_pushback.retry_after_remaining() == pytest.approx(CAP, abs=1)

    both = _lim("bid-both")  # in either order, the later path no longer widens the window
    proxy._maybe_pause_rejected("bid-both", both, _parse_unified(meta), meta=meta)
    proxy._note_retry_after_if_set(both, meta, "bid-both")
    assert both.retry_after_remaining() == pytest.approx(CAP, abs=1)


def test_604799_boundary_response_clears_within_the_cap(clock: _Clock) -> None:
    """(c) the 18:00:00 event: the bearer is routable again once the cap has elapsed."""
    lim = _lim("gmail")
    proxy._note_retry_after_if_set(lim, _meta(604799), "gmail")
    assert proxy._bearer_routing_retry_after("gmail", allow_retry_probe=True) is None
    clock.now += CAP + 1
    assert lim.retry_after_remaining() == 0
    assert proxy._bearer_routing_retry_after("gmail", allow_retry_probe=True) == 0.0
    assert lim.try_begin_retry_probe() is True
    assert lim.finish_retry_probe(success=True) is True  # an open account answers 2xx
    assert not lim.retry_probe_required()
    assert proxy._bearer_routing_retry_after("gmail", allow_retry_probe=False) == 0.0


def test_a_genuine_wall_is_probed_once_per_window(clock: _Clock) -> None:
    """(a) a real 7d wall stays closed, and exactly one request tests it per window."""
    lim = _lim("wall")
    wall = _meta(400000, reset_in=400000)  # a true weekly wall: rejected again on every probe
    proxy._note_retry_after_if_set(lim, wall, "wall")
    probes = 0
    for _tick in range(3600 // 30):
        clock.now += 30
        if proxy._bearer_routing_retry_after("wall", allow_retry_probe=True) is None:
            continue
        # window elapsed: several arrivals race for the half-open probe
        winners = sum(lim.try_begin_retry_probe() for _ in range(5))
        assert winners == 1
        assert proxy._bearer_routing_retry_after("wall", allow_retry_probe=True) is None
        probes += winners
        proxy._note_retry_after_if_set(lim, wall, "wall")  # the probe is refused: re-latch
        assert lim.finish_retry_probe(success=False) is True
        assert lim.retry_after_remaining() == pytest.approx(CAP, abs=1)
    # probes land at 900, 1800, 2700 and 3600 s: one per 900 s window, never zero or a stream
    assert probes == 4


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (_meta(600), 600.0),  # below the cap: untouched
        (_meta(604799, status_7d="allowed"), 604799.0),  # Fable-scoped (7d allowed): untouched
        ({"retry-after": "604799"}, 604799.0),  # no unified headers (API key, other lanes)
        ({**_meta(604799), PFX + "7d-status": "allowed_warning"}, 604799.0),
    ],
)
def test_only_an_explicit_7d_rejected_response_is_clamped(
    clock: _Clock, meta: dict, expected: float
) -> None:
    assert proxy._pushback_pause(meta, "bid")[0] == expected


def test_5h_rejected_keeps_the_upstream_window(clock: _Clock) -> None:
    meta = {**_meta(10000, status_7d="allowed"), PFX + "5h-status": "rejected"}
    assert proxy._pushback_pause(meta, "bid")[0] == 10000.0


def test_cap_zero_disables_the_clamp(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "RETRY_AFTER_REJECT_CAP_S", 0.0)
    assert proxy._pushback_pause(_meta(604799), "bid")[0] == 604799.0
