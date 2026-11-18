"""Hard routing gates precede weekly spill-only pacing; no real provider traffic."""

import time
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import accounts, config, limiter, proxy
from anthropic_throttle_proxy import usage_allocator as ua

MODEL = "claude-sonnet-5-5"


@pytest.fixture
async def pool(monkeypatch, proxy_admission_state):
    now = time.time()
    settings = {
        "ACCOUNT_ROUTING_MODE": "budget_paced",
        "ACCOUNT_CRED_PATHS": "hot:/fixture/hot,cool:/fixture/cool",
        "LOCAL_API_KEY_SENTINEL": "fixture-sentinel",
        "API_KEY_ROUTING_MODE": "off",
        "API_KEY_FILE": "",
        "METER_BINDINGS_VALID": True,
        "METER_BINDINGS": {},
        "METER_BINDING_REQUIRED": False,
        "WEEKLY_PACING_MODE": "enforce",
        "WEEKLY_PACING_LAW": "remaining",
        "WEEKLY_RATE_WINDOW_S": 1800.0,
        "WEEKLY_RATE_MARGIN": 0.1,
        "QUEUE_RETRY_AFTER_MAX_S": 300,
        "MAX_HOLD_RETRY_AFTER_S": 240.0,
        "QUEUE_MODE": "fair",
        "PRIORITY_RESERVE_SLOTS": 0,
        "STREAM_HOLD_ON_DEFER": False,
        "CREDENTIAL_STATE_FILE": "",
        "CLIENT_SHARES": {},
        "UPSTREAM": "http://127.0.0.1:1",
        "CENTRAL_URL": "",
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(proxy, "_restored_credentials", {})
    monkeypatch.setattr(proxy, "_WEEK_SAMPLES", ua.WeekSamples())
    monkeypatch.setattr(proxy, "_admission_closed", False)
    dispatch = AsyncMock(side_effect=AssertionError("pacing must refuse before dispatch"))
    monkeypatch.setattr(proxy, "_run_slot", dispatch)
    snapshot = [
        {"label": name, "bearer_id": name, "token": "synthetic-" + name} for name in ("hot", "cool")
    ]
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: list(snapshot))
    limits = {}
    for bid, util, reset in (("hot", 0.99, now + 3600), ("cool", 0.03, now + 6 * 86400)):
        limits[bid] = await proxy._get_bearer_limiter(bid, "fair", 2)
        assert limits[bid].try_begin_retry_probe()
        limits[bid].finish_retry_probe(success=True)
        config.bearer_state[bid]["unified"] = {
            "status_7d": "allowed_warning" if bid == "hot" else "allowed",
            "util_7d": util,
            "reset_7d": reset,
        }
        proxy._WEEK_SAMPLES.observe(bid, now - 1800, util if bid == "hot" else 0.01, reset)
        proxy._WEEK_SAMPLES.observe(bid, now, util, reset)
    limits["hot"].note_retry_after(3600)
    config.bearer_state["api-key"] = {"credential": {"ok": False}}
    return now, snapshot, limits, dispatch


@pytest.mark.parametrize("scoped_blocker", [False, True])
async def test_held_on_schedule_sibling_cannot_hide_eligible_siblings_pacing(
    pool,
    proxy_admission_app,
    scoped_blocker,
):
    now, snapshot, limits, dispatch = pool
    assert [s["state"] for s in proxy._weekly_pacing_states(snapshot, now)] == [
        "on_schedule",
        "ahead",
    ]
    assert proxy._account_routing_candidate_score(snapshot[0], "", allow_retry_probe=True) == float(
        "inf"
    )
    assert proxy._account_routing_candidate_score(snapshot[1], "", allow_retry_probe=True) < float(
        "inf"
    )
    if scoped_blocker:
        snapshot.append(
            {
                "label": "scoped",
                "bearer_id": "scoped",
                "token": "synthetic-scoped",
                "endpoint": {"usage": {"scoped": {"model": "sonnet", "util": 1.0}}},
            }
        )
        config.bearer_state["scoped"] = {
            "unified": {"util_7d": 0.01, "reset_7d": now + 86400},
        }
        for timestamp in (now - 1800, now):
            proxy._WEEK_SAMPLES.observe("scoped", timestamp, 0.01, now + 86400)
    async with TestClient(TestServer(proxy_admission_app)) as client:
        response = await client.post(
            "/v1/messages",
            json={"model": MODEL, "max_tokens": 16384, "messages": []},
            headers={"x-api-key": "fixture-sentinel"},
        )
        text = await response.text()
    assert response.status == 429
    assert text.startswith("weekly pacing:"), text
    assert response.headers[config.PACING_HEADER] == "1"
    assert response.headers[config.QUEUE_TIMEOUT_HEADER] == "1"
    assert response.headers["retry-after"] == "300"
    assert limits["hot"].retry_after_remaining() > 3500
    assert limiter.retry_probe_inflight("hot") is False
    assert config.bearer_state["cool"]["served"] == 0
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("mode", ["off", "observe"])
async def test_disabled_enforcement_does_not_filter_or_defer(pool, monkeypatch, mode):
    now, snapshot, limits, _ = pool
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", mode)
    assert proxy._weekly_pacing_filter(snapshot, now, model=MODEL) is snapshot
    assert proxy._weekly_pacing_defer(False, model=MODEL) is None
    assert limits["hot"].retry_after_remaining() > 3500


async def test_on_schedule_eligible_sibling_routes_without_clearing_hot_hold(pool):
    now, snapshot, limits, _ = pool
    samples = ua.WeekSamples()
    for account in snapshot:
        bid = account["bearer_id"]
        reading = config.bearer_state[bid]["unified"]
        for timestamp in (now - 1800, now):
            samples.observe(bid, timestamp, reading["util_7d"], reading["reset_7d"])
    proxy._WEEK_SAMPLES = samples
    assert proxy._weekly_pacing_defer(False, model=MODEL) is None
    headers = {"x-api-key": "fixture-sentinel"}
    bid, _ = proxy._route_account_if_enabled(
        headers,
        "api-key",
        method="POST",
        path="v1/messages",
        model=MODEL,
        allow_retry_probe=True,
    )
    assert bid == "cool"
    assert limits["hot"].retry_after_remaining() > 3500
    assert config.bearer_state["cool"]["served"] == 0


async def test_only_a_hard_eligible_spill_target_can_remove_the_ahead_account(pool):
    now, snapshot, limits, _ = pool
    ready = {"label": "ready", "bearer_id": "ready", "token": "synthetic-ready"}
    snapshot.append(ready)  # Existing unknown-burn behavior stays routable.
    config.bearer_state["ready"] = {"unified": {"util_7d": 0.01, "reset_7d": now + 86400}}
    assert proxy._weekly_pacing_filter(snapshot, now, model=MODEL) == [ready]
    assert proxy._weekly_pacing_defer(False, model=MODEL) is None
    assert limits["hot"].retry_after_remaining() > 3500


@pytest.mark.parametrize(
    "gate",
    [
        "credential",
        "rejected",
        "exhausted",
        "endpoint_exhausted",
        "endpoint_429",
        "scoped_exhausted",
        "probe_inflight",
        "cooldown",
        "meter_stale",
        "meter_exhausted",
    ],
)
async def test_no_hard_gate_is_relaxed_to_manufacture_pacing_capacity(pool, monkeypatch, gate):
    now, snapshot, limits, dispatch = pool
    state = config.bearer_state["cool"]
    if gate == "credential":
        state["credential"] = {"ok": False}
    elif gate == "rejected":
        state["unified"]["status_7d"] = "rejected"
    elif gate == "exhausted":
        state["unified"]["util_7d"] = 1.0
    elif gate == "endpoint_exhausted":
        snapshot[1]["endpoint"] = {"usage": {"util_5h": 1.0}}
    elif gate == "endpoint_429":
        snapshot[1]["endpoint"] = {"err": "usage (429)"}
    elif gate == "scoped_exhausted":
        snapshot[1]["endpoint"] = {"usage": {"scoped": {"model": "sonnet", "util": 1.0}}}
    elif gate == "probe_inflight":
        limits["cool"].require_retry_probe()
        assert limits["cool"].try_begin_retry_probe()
    elif gate == "cooldown":
        limits["cool"].note_retry_after(3600)
    else:
        monkeypatch.setattr(config, "METER_BINDINGS", {"cool": "fixture:cool"})
        monkeypatch.setattr(proxy, "_meter_bound_lane", lambda _bid: "fixture:cool")
        monkeypatch.setattr(
            proxy._lanes,
            "plan_meter_used_percent",
            lambda *_args: None if gate == "meter_stale" else 100.0,
        )
    assert proxy._weekly_pacing_eligible(snapshot, now, model=MODEL) == []
    assert proxy._weekly_pacing_defer(False, model=MODEL) is None
    assert limits["hot"].retry_after_remaining() > 3500
    dispatch.assert_not_awaited()


async def test_priority_exemption_does_not_clear_real_holds_or_claim_a_probe(pool):
    now, snapshot, limits, _ = pool
    limits["cool"].require_retry_probe()
    assert proxy._weekly_pacing_defer(True, model=MODEL) is None
    assert proxy._weekly_pacing_eligible(snapshot, now, model=MODEL) == [snapshot[1]]
    assert limiter.retry_probe_required("cool") is True
    assert limiter.retry_probe_inflight("cool") is False
    assert limits["hot"].retry_after_remaining() > 3500


async def test_soft_warning_and_short_cooldown_do_not_become_new_hard_gates(pool):
    now, snapshot, limits, _ = pool
    config.bearer_state["cool"]["unified"]["util_7d"] = 0.91
    limits["cool"].note_retry_after(3)
    assert proxy._weekly_pacing_eligible(snapshot, now, model=MODEL) == [snapshot[1]]
    assert proxy._weekly_pacing_defer(False, model=MODEL) == 300
    assert limits["cool"].retry_after_remaining() > 0
    assert limits["hot"].retry_after_remaining() > 3500


async def test_published_retry_matches_corrected_deferral_without_hiding_hot_account(pool):
    _, _, limits, _ = pool
    before = proxy._weekly_pacing_view()
    assert before["retry_after_s"] == proxy._weekly_pacing_defer(False) == 300
    assert {a["label"] for a in before["accounts"]} == {"hot", "cool"}
    assert limits["hot"].retry_after_remaining() > 3500
