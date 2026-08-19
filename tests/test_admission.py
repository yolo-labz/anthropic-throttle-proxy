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
