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
    saved_restored = dict(proxy._restored_credentials)
    config.bearer_state.clear()
    config.bearer_limiters.clear()
    proxy._restored_credentials.clear()
    config.state["upstream_egress_ok"] = True
    config.state.pop("upstream_auth_ok", None)
    yield
    config.bearer_state.clear()
    config.bearer_state.update(saved_state)
    config.bearer_limiters.clear()
    config.bearer_limiters.update(saved_limiters)
    proxy._restored_credentials.clear()
    proxy._restored_credentials.update(saved_restored)


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


async def test_live_credential_refusal_is_not_subscription_capacity():
    config.bearer_state["dead"] = {
        "unified": _unified(),
        "credential": {"ok": False, "status": 403, "reason": "org-refused"},
    }
    config.bearer_state["live"] = {"unified": _unified()}
    body = await _get()
    assert body["allow"] is True, body
    assert body["serving"] == 1
    assert body["selected"] == "live"
    assert body["bearers"]["dead"]["usable"] is False


async def test_credential_dead_bearer_does_not_false_roomy_saturation():
    config.bearer_state["dead"] = {
        "unified": _unified(),
        "credential": {"ok": False, "status": 403, "reason": "org-refused"},
    }
    config.bearer_state["live"] = {"unified": _unified()}
    config.bearer_limiters["dead"] = _Snap(max_concurrent=5, inflight=0, queued_total=0)
    config.bearer_limiters["live"] = _Snap(max_concurrent=5, inflight=5, queued_total=2)
    saturation = (await _get())["saturation"]
    assert saturation["usable_bearers"] == 1
    assert saturation["slots"] == 5
    assert saturation["all_usable_bearers_would_park"] is True


async def test_restored_credential_refusal_survives_restart_in_admission():
    config.bearer_state["dead"] = {"unified": _unified()}
    proxy._restored_credentials["dead"] = {
        "ok": False,
        "status": 403,
        "reason": "org-refused",
    }
    body = await _get()
    assert body["allow"] is False, body
    assert body["serving"] == 0
    assert body["bearers"]["dead"]["usable"] is False


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
        self._fields = {"queue_enabled": True, "priority_inflight": 0, **fields}

    def snapshot(self):
        return dict(self._fields)


async def test_saturation_reports_all_usable_pools_park_without_closing_lane():
    """The exact 23/08 shape remains open but warns bounded one-shots."""
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_state["b"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(max_concurrent=5, inflight=5, queued_total=3)
    config.bearer_limiters["b"] = _Snap(max_concurrent=5, inflight=5, queued_total=2)

    body = await _get()
    assert body["allow"] is True, body
    assert body["state"] == "open", body
    sat = body["saturation"]
    assert sat["slots"] == 10, sat
    assert sat["normal_inflight"] == 10, sat
    assert sat["queued"] == 5, sat
    assert sat["free"] == 0, sat
    assert sat["measured"] is True, sat
    assert sat["all_usable_bearers_would_park"] is True, sat


async def test_priority_traffic_does_not_fake_a_full_normal_pool():
    """The reserve dispatches outside the normal pool and must be subtracted."""
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(
        max_concurrent=5,
        inflight=6,
        priority_inflight=3,
        queued_total=0,
    )

    sat = (await _get())["saturation"]
    assert sat["normal_inflight"] == 3, sat
    assert sat["priority_inflight"] == 3, sat
    assert sat["free"] == 2, sat
    assert sat["all_usable_bearers_would_park"] is False, sat


async def test_a_free_slot_behind_a_queue_still_parks():
    """Dequeue is FIFO: arriving behind a queue parks even with a slot free."""
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(max_concurrent=5, inflight=3, queued_total=4)

    sat = (await _get())["saturation"]
    assert sat["free"] == 2, sat
    assert sat["all_usable_bearers_would_park"] is True, sat


async def test_one_roomy_bearer_keeps_lane_parking_hint_false():
    """A queue on one account cannot hide immediate room on a sibling."""
    config.bearer_state["full"] = {"unified": _unified()}
    config.bearer_state["roomy"] = {"unified": _unified()}
    config.bearer_limiters["full"] = _Snap(max_concurrent=5, inflight=5, queued_total=3)
    config.bearer_limiters["roomy"] = _Snap(max_concurrent=5, inflight=1, queued_total=0)

    sat = (await _get())["saturation"]
    assert sat["measured"] is True, sat
    assert sat["free"] == 4, sat
    assert sat["all_usable_bearers_would_park"] is False, sat


async def test_saturation_ignores_an_unusable_bearer_s_idle_slots():
    """A rejected bearer's idle slots are not usable capacity."""
    now = time.time()
    config.bearer_state["serving"] = {"unified": _unified()}
    config.bearer_state["rejected"] = {
        "unified": _unified(status_7d="rejected", util_7d=1.0, reset_7d=now + 600)
    }
    config.bearer_limiters["serving"] = _Snap(max_concurrent=5, inflight=5, queued_total=4)
    config.bearer_limiters["rejected"] = _Snap(max_concurrent=3, inflight=0, queued_total=0)

    sat = (await _get())["saturation"]
    assert sat["usable_bearers"] == 1, sat
    assert sat["slots"] == 5, sat
    assert sat["all_usable_bearers_would_park"] is True, sat


async def test_saturation_reports_room_when_the_lane_is_idle():
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(max_concurrent=5, inflight=1, queued_total=0)

    sat = (await _get())["saturation"]
    assert sat["free"] == 4, sat
    assert sat["measured"] is True, sat
    assert sat["all_usable_bearers_would_park"] is False, sat


async def test_queue_disabled_is_measured_and_never_parks():
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(queue_enabled=False)

    sat = (await _get())["saturation"]
    assert sat["measured"] is True, sat
    assert sat["all_usable_bearers_would_park"] is False, sat


async def test_no_limiter_is_unmeasured_and_fails_open():
    """A consumer must not act on a reading the proxy never took."""
    config.bearer_state["a"] = {"unified": _unified()}

    sat = (await _get())["saturation"]
    assert sat["usable_bearers"] == 1, sat
    assert sat["measured_bearers"] == 0, sat
    assert sat["measured"] is False, sat
    assert sat["all_usable_bearers_would_park"] is False, sat


@pytest.mark.parametrize(
    "fields",
    [
        {"inflight": 9, "queued_total": 6},
        {"max_concurrent": 5, "queued_total": 6},
    ],
)
async def test_partial_snapshot_is_unmeasured_not_roomy(fields):
    """Every field in the normal-pool predicate is required."""
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(**fields)

    sat = (await _get())["saturation"]
    assert sat["measured"] is False, sat
    assert sat["all_usable_bearers_would_park"] is False, sat


async def test_one_unmeasured_bearer_makes_the_lane_hint_unmeasured():
    """A full measured subset cannot speak for an unmeasured usable sibling."""
    config.bearer_state["full"] = {"unified": _unified()}
    config.bearer_state["unknown"] = {"unified": _unified()}
    config.bearer_limiters["full"] = _Snap(max_concurrent=5, inflight=5, queued_total=3)

    sat = (await _get())["saturation"]
    assert sat["usable_bearers"] == 2, sat
    assert sat["measured_bearers"] == 1, sat
    assert sat["measured"] is False, sat
    assert sat["all_usable_bearers_would_park"] is False, sat


async def test_saturation_survives_a_garbage_limiter_snapshot():
    """Invariant #4: this endpoint is polled; a bad field must not raise."""
    config.bearer_state["a"] = {"unified": _unified()}
    config.bearer_limiters["a"] = _Snap(max_concurrent="?", inflight=None, queued_total=-3)

    sat = (await _get())["saturation"]
    assert sat["measured"] is False, sat
    assert sat["all_usable_bearers_would_park"] is False, sat
