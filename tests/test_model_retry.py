"""A Fable-only rejection must not freeze other models or hide an early reset."""

import asyncio
import json
import math
import time

import pytest

from anthropic_throttle_proxy import config, limiter, model_retry, proxy, ratelimit

MODEL = "claude-fable-5-2"
PREFIX = "anthropic-ratelimit-unified-"


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setattr(config, "RETRY_AFTER_RESTORE_CAP_S", 0)
    model_retry._inflight.clear()
    model_retry._last_checks.clear()
    model_retry._versions.clear()
    value = limiter.FairBearerLimiter(4, "fair", "account")
    limiter.clear_retry_probe("account")
    monkeypatch.setitem(config.bearer_limiters, "account", value)
    monkeypatch.setitem(config.bearer_state, "account", {})
    return value


def rejected(**changes):
    meta = {
        "retry-after": "200000",
        PREFIX + "status": "rejected",
        PREFIX + "representative-claim": "seven_day",
        PREFIX + "reset": str(int(time.time() + 200000)),
        PREFIX + "7d-status": "allowed",
        PREFIX + "7d-utilization": "0.54",
        PREFIX + "7d-reset": str(int(time.time() + 200000)),
        PREFIX + "7d_oi-status": "rejected",
        PREFIX + "7d_oi-utilization": "1",
        PREFIX + "7d_oi-reset": str(int(time.time() + 200000)),
    }
    return meta | changes


def success(model=MODEL):
    return (
        200,
        json.dumps(
            {
                "type": "message",
                "role": "assistant",
                "model": model,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "OK"}],
            }
        ).encode(),
        {PREFIX + "status": "allowed"},
    )


async def test_fable_only_wall_does_not_pause_week_or_shrink_shared_capacity(base):
    meta = rejected()
    initial_cap = base.max_concurrent
    await proxy._apply_unified("account", config.bearer_state["account"], base, meta, model=MODEL)
    attempt = proxy._Attempt()
    attempt.final_status = 429
    attempt.meta = meta
    attempt.context = {"model": MODEL}
    await proxy._aimd_feedback("account", base, attempt)
    assert base.retry_after_remaining() == 0
    assert base.max_concurrent == initial_cap
    assert not base.retry_probe_required()
    assert model_retry.scoped_remaining("account", MODEL) > 190000
    assert model_retry.scoped_remaining("account", "claude-opus-5-5") == 0
    assert model_retry.scoped_remaining("account", "claude-sonnet-5-5") == 0
    acct = {"bearer_id": "account", "endpoint": {"usage": {"util_7d": 0.54}}}
    assert math.isinf(proxy._account_routing_candidate_score(acct, "account", model=MODEL))
    assert math.isfinite(
        proxy._account_routing_candidate_score(
            acct, "account", model="claude-opus-5-5", allow_pressure=True
        )
    )
    limiter.flush_retry_after_writes()
    limiter._retry_after_state = None
    assert model_retry.scoped_remaining("account", MODEL) > 190000


@pytest.mark.parametrize(
    "changes",
    [
        {PREFIX + "7d-status": "rejected", PREFIX + "7d-utilization": "1"},
        {PREFIX + "7d_oi-status": "unknown"},
        {PREFIX + "5h-status": "rejected", PREFIX + "5h-utilization": "1"},
        {PREFIX + "7d-utilization": "nan"},
    ],
)
async def test_whole_week_or_unknown_rejection_stays_global(base, changes):
    assert model_retry.note_pause(base, 200000, rejected(**changes), MODEL) is False
    assert base.retry_after_remaining() > 190000
    assert base.retry_probe_required()


async def test_successful_early_reset_keeps_normal_message_single_flight_gate(base):
    model_retry.note_pause(base, 200000, None)

    async def send():
        return success()

    assert await model_retry.recheck(base, MODEL, send)
    assert base.retry_after_remaining() == 0
    assert base.retry_probe_required()
    assert base.try_begin_retry_probe() is True
    assert base.try_begin_retry_probe() is False


async def test_reset_of_scoped_bucket_preserves_real_whole_account_hold(base):
    model_retry.note_pause(base, 200000, rejected(), MODEL)

    async def send():
        # A real whole-week rejection arriving during the probe must win.
        model_retry.note_pause(base, 250000, None)
        return success()

    await model_retry.recheck(base, MODEL, send)
    assert base.retry_after_remaining() > 240000


async def test_repeated_equal_deadline_rejection_invalidates_probe_snapshot(base):
    model_retry.note_pause(base, 200000, None)
    until = base._retry_after_until

    async def send():
        # Extending is unnecessary: this is newer evidence about the SAME reset.
        model_retry.note_pause(base, 1, None)
        assert base._retry_after_until == until
        return success()

    assert await model_retry.recheck(base, MODEL, send) is None
    assert base.retry_after_remaining() > 190000


@pytest.mark.parametrize("persistent", [True, False])
async def test_scoped_reset_does_not_clear_other_account(base, monkeypatch, persistent):
    if not persistent:
        monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", "")
    model_retry.note_pause(base, 200000, rejected(), MODEL)
    limiter._load_retry_after_state()["other"] = time.time() + 250000
    assert model_retry.scoped_remaining("account", MODEL) > 190000

    async def send():
        return success()

    assert await model_retry.recheck(base, MODEL, send)
    assert model_retry.scoped_remaining("account", MODEL) == 0
    assert limiter._load_retry_after_state()["other"] > time.time() + 240000


async def test_probe_is_single_flight_and_failed_probe_has_bounded_cadence(base):
    model_retry.note_pause(base, 200000, None)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def send():
        calls.append(MODEL)
        entered.set()
        await release.wait()
        return 429, b"{}", {}

    task = asyncio.create_task(model_retry.recheck(base, MODEL, send))
    await entered.wait()
    sibling = asyncio.create_task(model_retry.recheck(base, MODEL, send))
    await asyncio.sleep(0)
    assert not sibling.done()
    release.set()
    await asyncio.gather(task, sibling)
    assert await model_retry.recheck(base, MODEL, send) is None
    assert calls == [MODEL]
    assert base.retry_after_remaining() > 190000


async def test_wrong_model_success_cannot_clear_selected_model_hold(base):
    model_retry.note_pause(base, 200000, rejected(), MODEL)

    async def send():
        return success("claude-opus-5-5")

    assert await model_retry.recheck(base, MODEL, send) is None
    assert model_retry.scoped_remaining("account", MODEL) > 190000


def test_scoped_headers_survive_forwarding_extraction_and_parsing():
    meta = ratelimit._extract_ratelimit(rejected())
    parsed = ratelimit._parse_unified(meta)
    assert parsed["status_7d_oi"] == "rejected"
    assert parsed["util_7d_oi"] == 1


@pytest.fixture
def configured_subscription(base, monkeypatch):
    from anthropic_throttle_proxy import accounts

    model_retry.note_pause(base, 200000, None)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(proxy, "_meter_binding_allows", lambda _: True)
    monkeypatch.setattr(
        accounts,
        "routing_snapshot",
        lambda: [
            {"bearer_id": "account", "token": "subscription-fixture"},
        ],
    )


@pytest.mark.parametrize("model", [MODEL, "claude-opus-5-5"])
async def test_demand_probe_uses_configured_subscription_exact_model_and_budget(
    base, monkeypatch, model, configured_subscription
):
    prospective = object()
    calls = []

    async def transport(admission, url, headers, payload, client_timeout):
        assert admission is prospective
        assert url == config.UPSTREAM.rstrip("/") + proxy.MESSAGES_PATH
        assert headers["authorization"] == "Bearer subscription-fixture"
        assert "x-api-key" not in headers
        assert payload["model"] == model
        if model == MODEL:
            assert payload["max_tokens"] == 64
            assert payload["thinking"] == {"type": "adaptive"}
            assert payload["output_config"] == {"effort": "low"}
        else:
            assert payload["max_tokens"] == 8
            assert "thinking" not in payload and "output_config" not in payload
        calls.append(model)
        return success(model)

    monkeypatch.setattr(proxy, "_post_internal_probe_headers", transport)
    await proxy._recheck_subscription_pause(
        base, model, {"authorization": "Bearer subscription-fixture"}, prospective
    )
    assert calls == [model]
    assert base.retry_after_remaining() == 0
    assert base.retry_probe_required()


async def test_unregistered_bearer_never_funds_a_probe(base, monkeypatch, configured_subscription):

    async def forbidden(*args):
        pytest.fail("an arbitrary API token cannot fund recovery")

    monkeypatch.setattr(proxy, "_post_internal_probe_headers", forbidden)
    await proxy._recheck_subscription_pause(
        base, MODEL, {"authorization": "Bearer unregistered-token"}, None
    )
    assert base.retry_after_remaining() > 190000


async def test_scoped_wall_arriving_after_selection_reroutes_same_model(base, monkeypatch):
    model_retry.note_pause(base, 200000, rejected(), MODEL)
    calls = []

    async def inconclusive(*args):
        return None

    async def wait_reval(callback):
        await callback()
        return True

    def alternate(bid, remaining, source):
        calls.append((bid, source))
        return "sibling", {"authorization": "Bearer sibling-subscription"}

    monkeypatch.setattr(proxy, "_recheck_subscription_pause", inconclusive)
    monkeypatch.setattr(proxy, "_meter_binding_allows", lambda _: True)
    result = await proxy._pre_dispatch_gate(
        path="v1/messages",
        cid="client",
        bid="account",
        headers={},
        model=MODEL,
        probe_lease={"bid": ""},
        finish_probe=lambda **kwargs: None,
        try_reroute=alternate,
        wait_alternate=inconclusive,
        reroute=lambda: None,
        wait_reval=wait_reval,
        meter_wait_bounded=True,
    )
    assert result[1] == "sibling"
    assert result[3] is None and result[4] == "reroute"
    assert calls == [("account", "model-pre-dispatch")]


async def test_real_handler_revalidates_when_every_account_is_long_held(
    proxy_admission_app, monkeypatch
):
    from aiohttp import web

    from anthropic_throttle_proxy import accounts

    model_retry._last_checks.clear()
    model_retry._inflight.clear()
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "least_loaded")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "fixture")
    monkeypatch.setattr(config, "METER_BINDINGS", {})
    monkeypatch.setattr(config, "METER_BINDING_REQUIRED", False)
    monkeypatch.setattr(config, "METER_BINDINGS_VALID", True)
    monkeypatch.setattr(config, "PLAN_METER_LANE", "")
    monkeypatch.setattr(config, "QUEUE_MODE", "off")
    rows = []
    for token in ("subscription-one", "subscription-two"):
        bid = proxy._bearer_id({"Authorization": "Bearer " + token})
        value = await proxy._get_bearer_limiter(bid, "off", 2)
        model_retry.note_pause(value, 200000, None)
        rows.append({"bearer_id": bid, "token": token, "label": token})
    monkeypatch.setattr(accounts, "routing_snapshot", lambda now=None: rows)
    probes, forwards = [], []

    async def transport(admission, url, headers, payload, client_timeout):
        probes.append((headers["authorization"], payload["model"]))
        return success()

    async def forward(request, headers, body, url, client_timeout, attempt, **kwargs):
        forwards.append(
            (headers.get("Authorization", headers.get("authorization")), json.loads(body)["model"])
        )
        attempt.final_status = 200
        attempt.meta = {}
        attempt.response = web.json_response({"ok": True})
        return attempt.response, None

    monkeypatch.setattr(proxy, "_post_internal_probe_headers", transport)
    monkeypatch.setattr(proxy, "_try_forward", forward)
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(proxy_admission_app))
    await client.start_server()
    response = await client.post(
        "/v1/messages",
        headers={"Authorization": "Bearer subscription-one"},
        json={"model": MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status == 200, await response.text()
    assert probes == [("Bearer subscription-one", MODEL)]
    assert forwards == probes
    await client.close()
    assert config.bearer_limiters[rows[0]["bearer_id"]].retry_after_remaining() == 0
    assert config.bearer_limiters[rows[1]["bearer_id"]].retry_after_remaining() > 190000
