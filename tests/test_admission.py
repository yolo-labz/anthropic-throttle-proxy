"""GET /__throttle/admission — the authoritative availability verdict.

The endpoint exists because consumers that re-derive availability from
/__throttle/health get it wrong. On 19/08/2026 pi's own oracle refused the
whole fleet for two hours on `allowed_warning` at 76% of the weekly window,
while this proxy was serving that same bearer normally. These cases pin the
difference so the verdict cannot drift back.
"""

from __future__ import annotations

import json
import time

import pytest

from anthropic_throttle_proxy import config, proxy


@pytest.fixture(autouse=True)
def _clean_bearers():
    """Each case owns the bearer table outright."""
    saved_state = dict(config.bearer_state)
    saved_limiters = dict(config.bearer_limiters)
    config.bearer_state.clear()
    config.bearer_limiters.clear()
    config.state["upstream_egress_ok"] = True
    config.state.pop("upstream_auth_ok", None)
    yield
    config.bearer_state.clear()
    config.bearer_state.update(saved_state)
    config.bearer_limiters.clear()
    config.bearer_limiters.update(saved_limiters)


def _unified(**over):
    now = time.time()
    base = {
        "status_5h": "allowed",
        "status_7d": "allowed",
        "util_5h": 0.2,
        "util_7d": 0.4,
        "reset_5h": now + 3600,
        "reset_7d": now + 86400,
    }
    base.update(over)
    return base


async def _get() -> dict:
    """Call the handler directly — same pattern the health tests use."""
    resp = await proxy.admission(None)
    assert resp.status == 200
    return json.loads(resp.body)


async def test_warning_bearer_is_open():
    """The exact 19/08 shape: one serving bearer, flagged, far from the wall.

    pi refused the fleet here. The proxy must not: `allowed_warning` is a flag,
    not a stop, and `bearer_usable` gates only on `rejected` / retry-after.
    """
    config.bearer_state["47f0b262"] = {
        "unified": _unified(status_7d="allowed_warning", util_7d=0.76, util_5h=0.38)
    }
    body = await _get()
    assert body["allow"] is True, body
    assert body["state"] == "open", body
    assert body["serving"] == 1
    assert body["selected"] == "47f0b262"
    assert body["retry_after_s"] == 0


async def test_only_serving_bearer_warning_still_open():
    """Concentration onto ONE flagged account is when the lane is needed most."""
    config.bearer_state["a"] = {"unified": _unified(status_7d="rejected", util_7d=1.0)}
    config.bearer_state["b"] = {"unified": _unified(status_7d="rejected", util_7d=1.0)}
    config.bearer_state["c"] = {"unified": _unified(status_7d="allowed_warning", util_7d=0.76)}
    body = await _get()
    assert body["allow"] is True, body
    assert body["serving"] == 1
    assert body["total"] == 3


async def test_all_rejected_is_capped_with_a_due_time():
    """Conclusive stop — and it must say WHEN, so a consumer waits not refuses."""
    now = time.time()
    config.bearer_state["a"] = {
        "unified": _unified(status_7d="rejected", util_7d=1.0, reset_7d=now + 600)
    }
    config.bearer_state["b"] = {
        "unified": _unified(status_7d="rejected", util_7d=1.0, reset_7d=now + 1800)
    }
    body = await _get()
    assert body["allow"] is False, body
    assert body["state"] == "capped"
    # soonest of the two, not the latest
    assert 500 < body["retry_after_s"] <= 600, body


async def test_retry_after_pauses_a_bearer():
    """An active Retry-After is a lock; it must close and report its remainder."""
    now = time.time()

    class _Lim:
        def snapshot(self):
            return {"retry_after_until": now + 42}

    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Lim()
    body = await _get()
    assert body["allow"] is False, body
    assert 40 < body["retry_after_s"] <= 42, body


async def test_egress_down_is_capped():
    """A healthy bearer cannot help if the lane cannot reach upstream."""
    config.bearer_state["a"] = {"unified": _unified()}
    config.state["upstream_egress_ok"] = False
    body = await _get()
    assert body["allow"] is False, body
    assert "upstream-egress-down" in body["reason"], body


async def test_anon_and_api_key_are_not_subscription_bearers():
    """`_anon` is the unauthenticated bypass slot; `api-key` is pay-go.

    Counting either would let health-check traffic answer "yes, a subscription
    can serve", which is the question the endpoint is actually asked.
    """
    config.bearer_state["_anon"] = {"unified": _unified()}
    config.bearer_state[proxy.API_KEY_BEARER_ID] = {"unified": _unified()}
    body = await _get()
    assert body["total"] == 0, body
    assert body["allow"] is False, body
    assert body["reason"] == "no bearers observed yet", body


async def test_stale_rejected_window_does_not_lock():
    """A `rejected` window whose reset has PASSED is a stale reading, not a lock.

    `unified_live_view` already drops it for the hot path; the verdict must
    inherit that rather than re-implementing the rule.
    """
    now = time.time()
    config.bearer_state["a"] = {
        "unified": _unified(status_7d="rejected", util_7d=1.0, reset_7d=now - 5)
    }
    body = await _get()
    assert body["allow"] is True, body


class _Snap:
    """A limiter stub that reports only the scheduler fields admission reads."""

    def __init__(self, **fields):
        self._fields = fields

    def snapshot(self):
        return dict(self._fields)


async def test_saturation_reports_a_full_lane_without_closing_it():
    """The 23/08 shape: every usable slot busy, a queue behind it, `allow` true.

    A queued request IS served, so tightening `allow` here would repeat the
    19/08 mistake. The verdict stays open and the pressure is reported, which
    is what lets a bounded one-shot pick another lane instead of parking.
    """
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_state["b"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(max_concurrent=5, inflight=5, queued_total=3)
    config.bearer_limiters["b"] = _Snap(max_concurrent=5, inflight=6, queued_total=2)
    body = await _get()
    assert body["allow"] is True, body
    assert body["state"] == "open", body
    sat = body["saturation"]
    assert sat["slots"] == 10, sat
    assert sat["inflight"] == 11, sat
    assert sat["queued"] == 5, sat
    assert sat["free"] == 0, sat
    assert sat["full"] is True, sat


async def test_saturation_ignores_an_unusable_bearer_s_idle_slots():
    """A rejected bearer's free slots are not capacity.

    Counting them reported the starved lane that caused this incident as
    roomy: 47f0b262 sat at 7d=rejected with 0 inflight while the two serving
    accounts were full.
    """
    now = time.time()
    config.bearer_state["serving"] = {"unified": _unified()}
    config.bearer_state["rejected"] = {
        "unified": _unified(status_7d="rejected", util_7d=1.0, reset_7d=now + 600)
    }
    config.bearer_limiters["serving"] = _Snap(max_concurrent=5, inflight=5, queued_total=4)
    config.bearer_limiters["rejected"] = _Snap(max_concurrent=3, inflight=0, queued_total=0)
    sat = (await _get())["saturation"]
    assert sat["slots"] == 5, sat
    assert sat["inflight"] == 5, sat
    assert sat["free"] == 0, sat
    assert sat["full"] is True, sat


async def test_saturation_reports_free_slots_when_the_lane_has_room():
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(max_concurrent=5, inflight=1, queued_total=0)
    sat = (await _get())["saturation"]
    assert sat["free"] == 4, sat
    assert sat["full"] is False, sat


async def test_saturation_is_not_full_when_no_limiter_has_reported():
    """Unknown is not full. A consumer must not refuse on a reading never taken."""
    config.bearer_state["a"] = {"unified": _unified()}
    sat = (await _get())["saturation"]
    assert sat["slots"] == 0, sat
    assert sat["full"] is False, sat


async def test_saturation_survives_a_garbage_limiter_snapshot():
    """Invariant #4: this endpoint is polled; a bad field must not raise."""
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(max_concurrent="?", inflight=None, queued_total=-3)
    sat = (await _get())["saturation"]
    assert sat == {
        "slots": 0,
        "inflight": 0,
        "queued": 0,
        "free": 0,
        "full": False,
        "queue_max_wait_s": float(config.QUEUE_MAX_WAIT_S),
    }, sat
