"""Spec 335 — free-tier budget meter: declared window, admission, no-AIMD.

Every budget value here is MOCKED (``free_budget.load_from_env`` with an explicit
mapping) because the vendor publishes none: OpenCode's free models are listed
``Unlimited``/``Free`` and the gateway sends no quota header on 200 or 429
(``specs/opencode-free-tier-limits-2026-10-19.md`` §2.3/§2.5 + the probe receipt
in ``specs/335-free-budget-admission/research.md``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, free_budget, proxy
from anthropic_throttle_proxy.limiter import FairBearerLimiter

MODEL = "glm-5.3-flash-free"
BODY = json.dumps(
    {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
).encode()
SSE = b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


@pytest.fixture(autouse=True)
def inert_budget():
    """Each test declares its own budget; never inherit another test's state."""
    free_budget.reset_for_tests()
    yield
    free_budget.reset_for_tests()


def declare(*, models: str, **extra: str) -> None:
    free_budget.load_from_env(
        {"THROTTLE_FREE_LANE": "go", "THROTTLE_FREE_BUDGETS": models, **extra}
    )


# --------------------------------------------------------------------------
# The meter itself (mocked budget state)
# --------------------------------------------------------------------------


def test_disabled_instance_is_inert():
    free_budget.reset_for_tests()
    assert free_budget.enabled() is False
    assert free_budget.admit(MODEL, now=100.0).allowed is True
    assert free_budget.admit(MODEL, now=100.0).reason == "disabled"
    # No evidence ⇒ never claim headroom either (UNKNOWN IS NOT HEADROOM).
    assert free_budget.budget_blocks(MODEL, now=100.0) is None


def test_declared_window_counts_then_fails_fast_at_the_ceiling():
    declare(models=f"{MODEL}=3/86400")
    for i in range(3):
        decision = free_budget.admit_and_count(MODEL, now=1_000.0 + i)
        assert decision.allowed is True
        assert decision.used == i + 1
        assert decision.budget == 3

    refused = free_budget.admit_and_count(MODEL, now=1_004.0)
    assert refused.allowed is False
    assert refused.reason == "budget-exhausted"
    # Honest retry_after = time to window end (not a made-up constant).
    assert refused.retry_after_s == 1_000 + 86400 - 1_004
    assert refused.used == 3, "a refusal must not claim a unit"
    assert refused.refusal is not None
    assert refused.refusal.reason.value == "exhausted"
    assert refused.refusal.budget_label == "go-" + MODEL


def test_pre_dispatch_check_does_not_consume():
    declare(models=f"{MODEL}=1/86400")
    assert free_budget.admit(MODEL, now=10.0).allowed is True
    assert free_budget.admit(MODEL, now=10.0).allowed is True, "a check spends nothing"
    assert free_budget.admit_and_count(MODEL, now=10.0).used == 1
    assert free_budget.admit(MODEL, now=10.0).allowed is False


def test_window_rollover_reopens_the_allowance():
    declare(models=f"{MODEL}=1/600")
    assert free_budget.admit_and_count(MODEL, now=100.0).allowed is True
    assert free_budget.admit(MODEL, now=699.0).allowed is False
    reopened = free_budget.admit_and_count(MODEL, now=700.0)
    assert reopened.allowed is True
    assert reopened.used == 1


def test_a_clock_step_backwards_reopens_instead_of_freezing():
    declare(models=f"{MODEL}=1/600")
    assert free_budget.admit_and_count(MODEL, now=1_000.0).allowed is True
    assert free_budget.admit(MODEL, now=900.0).allowed is True, "negative age is not freshness"


def test_pressure_line_classifies_without_closing_admission():
    """At 90 % a pushback may be READ as budget; dispatch still runs to 100 %."""
    declare(models=f"{MODEL}=10/600")
    for i in range(8):
        free_budget.admit_and_count(MODEL, now=100.0 + i)
    assert free_budget.budget_blocks(MODEL, now=101.0) is False, "headroom is not budget"
    free_budget.admit_and_count(MODEL, now=110.0)  # 9/10 = the pressure line
    assert free_budget.budget_blocks(MODEL, now=111.0) is True, "at the line"
    assert free_budget.admit(MODEL, now=111.0).allowed is True, "line != ceiling"
    free_budget.admit_and_count(MODEL, now=111.0)  # 10/10 = the ceiling
    assert free_budget.admit(MODEL, now=112.0).allowed is False, "100 % is the ceiling"


def test_blocks_is_three_valued_and_undeclared_models_stay_unknown():
    declare(models=f"{MODEL}=1/600")
    assert free_budget.budget_blocks(MODEL, now=100.0) is False
    free_budget.admit_and_count(MODEL, now=100.0)
    assert free_budget.budget_blocks(MODEL, now=101.0) is True
    assert free_budget.budget_blocks("longcat-2.5-preview-free", now=101.0) is None
    # Aggregate (no model): the WALLEST declared model decides, conservatively.
    assert free_budget.budget_blocks("", now=101.0) is True


def test_required_mode_refuses_undeclared_models():
    declare(models=f"{MODEL}=5/600", THROTTLE_FREE_BUDGET_REQUIRED="1")
    decision = free_budget.admit("longcat-2.5-preview-free", now=10.0)
    assert decision.allowed is False
    assert decision.reason == "undeclared"
    assert decision.refusal.reason.value == "unbound"
    assert free_budget.budget_blocks("longcat-2.5-preview-free", now=10.0) is True


def test_malformed_declaration_fails_closed_and_is_logged(capsys):
    free_budget.load_from_env(
        {"THROTTLE_FREE_LANE": "go", "THROTTLE_FREE_BUDGETS": "not-a-spec,x=0/10,ok=3/600"}
    )
    decision = free_budget.admit(MODEL, now=1.0)
    assert decision.allowed is False, "a bad entry must never vanish into feature-off"
    assert decision.reason == "invalid-config"
    assert decision.refusal.reason.value == "unknown"
    assert free_budget.budget_blocks(MODEL, now=1.0) is True
    assert "free-budget-config-error" in capsys.readouterr().err


def test_lane_name_is_derived_from_the_upstream_when_not_declared():
    free_budget.load_from_env(
        {
            "THROTTLE_UPSTREAM": "https://opencode.ai/inference/go",
            "THROTTLE_FREE_BUDGETS": "m=1/60",
        }
    )
    assert free_budget.snapshot()["lane"] == "go"
    declare(models="m=1/60")
    assert free_budget.snapshot()["lane"] == "go", "explicit lane wins"


# --------------------------------------------------------------------------
# Admission wiring (real handler, local fake upstream)
# --------------------------------------------------------------------------


async def _gate(
    monkeypatch,
    proxy_admission_app,
    *,
    budget_models: str,
    calls: int = 1,
) -> tuple[list[SimpleNamespace], int]:
    """Run ``calls`` POSTs through the real admission handler.

    Returns the observed responses (status/headers/payload copied inside the
    connection scope) and how many requests the fake upstream actually served.
    """
    upstream_calls = {"n": 0}

    async def upstream(request):
        upstream_calls["n"] += 1
        await request.read()
        return web.Response(body=SSE, content_type="text/event-stream")

    remote = web.Application()
    remote.router.add_post("/v1/chat/completions", upstream)
    async with TestServer(remote) as origin:
        for key, value in {
            "UPSTREAM": str(origin.make_url("")).rstrip("/"),
            "CENTRAL_URL": "",
            "QUEUE_MODE": "fair",
            "MAX_CONCURRENT": 4,
            "AIMD_INITIAL_CONCURRENT": 4,
            "PRIORITY_RESERVE_SLOTS": 0,
            "RATE_PUSHBACK_RETRIES": 0,
            "MIN_DISPATCH_GAP_S": 0,
            "QUEUE_MAX_WAIT_S": 3,
        }.items():
            monkeypatch.setattr(config, key, value)
        monkeypatch.setenv("THROTTLE_ACCOUNT_ROUTING", "off")
        declare(models=budget_models)
        results: list[SimpleNamespace] = []
        async with TestClient(TestServer(proxy_admission_app)) as client:
            for _ in range(calls):
                async with client.post(
                    "/v1/chat/completions",
                    data=BODY,
                    headers={"Authorization": "Bearer test-key"},
                ) as response:
                    results.append(
                        SimpleNamespace(
                            status=response.status,
                            # Normalised: the wire sends ``Retry-After`` and a
                            # plain dict is case-sensitive (CIMultiDict is not).
                            headers={k.lower(): v for k, v in response.headers.items()},
                            payload=await response.read(),
                        )
                    )
    return results, upstream_calls["n"]


async def test_exhausted_budget_fails_fast_without_a_slot_or_an_upstream_call(
    monkeypatch, proxy_admission_app
):
    """The mission's core ask: never cross the line — and fail FAST when spent."""
    results, served = await _gate(
        monkeypatch, proxy_admission_app, budget_models=f"{MODEL}=1/600", calls=2
    )
    assert [r.status for r in results] == [200, 503], results[1].payload[:400]

    refusal = results[1]
    assert refusal.headers["x-throttle-prospective-refusal"] == "1"
    assert refusal.headers["x-throttle-refusal-source"] == "local"
    assert refusal.headers["x-throttle-prospective-reason"] == "exhausted"
    assert refusal.headers["x-throttle-prospective-budget"] == f"go-{MODEL}"
    assert 1 <= int(refusal.headers["retry-after"]) <= 600
    body = json.loads(refusal.payload)
    assert body["error"]["type"] == "prospective_admission_refused"
    assert body["error"]["budget"] == f"go-{MODEL}"
    assert 1 <= body["error"]["retry_after_s"] <= 600

    # One upstream attempt only: the refused request never bought a dispatch.
    assert served == 1
    # And no AIMD movement: the ceiling is exactly what it was configured to.
    ceilings = [lim.max_concurrent for lim in config.bearer_limiters.values()]
    assert ceilings == [4], ceilings
    assert config.state["inflight"] == config.state["queued"] == 0, "no slot left behind"


async def test_undeclared_models_are_unchanged_by_the_meter(monkeypatch, proxy_admission_app):
    results, served = await _gate(
        monkeypatch, proxy_admission_app, budget_models="other-model=1/600", calls=1
    )
    assert results[0].status == 200, results[0].payload[:400]
    assert served == 1
    assert "x-throttle-prospective-refusal" not in results[0].headers


# --------------------------------------------------------------------------
# Pushback classification + the AIMD floor (mocked budget state)
# --------------------------------------------------------------------------


def test_pushback_evidence_reads_the_declared_budget(monkeypatch):
    monkeypatch.setattr(config, "PLAN_METER_LANE", "")
    monkeypatch.setattr(config, "METER_BINDINGS", {})
    free_budget.reset_for_tests()
    assert proxy._pushback_evidence(None, "") == "unknown", "no declaration ⇒ no evidence"

    declare(models=f"{MODEL}=1/600")
    assert proxy._pushback_evidence(None, "") == "unknown", "headroom is not evidence"
    # Counted on the REAL clock: the classifier consults without a clock, so a
    # synthetic timestamp would be a window the caller has already lived past.
    free_budget.admit_and_count(MODEL)
    assert proxy._pushback_evidence(None, "") == "budget", "an exhausted window IS evidence"
    # Exhausted ⇒ the boolean wrapper keeps its historical conservative answer.
    assert proxy._budget_under_pressure(None, "") is True


async def test_budget_pushback_never_drives_the_ceiling_below_the_floor(monkeypatch):
    """AIMD_MIN is a floor under EVERY budget-classified pushback, not a target."""
    monkeypatch.setattr(config, "AIMD_MIN", 3)
    monkeypatch.setattr(config, "AIMD_INITIAL_CONCURRENT", 8)
    monkeypatch.setattr(config, "AIMD_DECREASE", 0.5)
    monkeypatch.setattr(config, "AIMD_BACKOFF_S", 0.0)
    monkeypatch.setattr(config, "PLAN_METER_LANE", "")
    monkeypatch.setattr(config, "METER_BINDINGS", {})
    declare(models=f"{MODEL}=1/600")
    free_budget.admit_and_count(MODEL)  # real clock: the classifier reads it back

    lim = FairBearerLimiter(8, "fair", bearer_id="budget-bear")
    assert lim.max_concurrent == 8
    attempt = proxy._Attempt()
    attempt.context = {"model": MODEL}
    seen = [lim.max_concurrent]
    for _ in range(10):  # the incident shape: repeated headerless budget 429s
        await proxy._aimd_apply_pushback("budget-bear", lim, attempt, 429, 0.0)
        seen.append(lim.max_concurrent)
    assert min(seen) >= 3, f"floor violated: {seen}"
    assert seen[-1] == 3, f"must settle AT the floor, never below it: {seen}"


async def test_budget_burst_is_capped_at_the_declared_allowance(
    monkeypatch, proxy_admission_app
):
    """Whatever seam refuses, the invariant is one: never spend past the line.

    Six arrivals against a declared allowance of 2 must produce at
    most TWO upstream dispatches — a pushback-retry that re-dispatched a local
    refusal would be visible here as ``served > budget``.
    """
    results, served = await _gate(
        monkeypatch, proxy_admission_app, budget_models=f"{MODEL}=2/600", calls=6
    )
    assert served == 2, f"declared budget breached: upstream served {served}"
    statuses = sorted(r.status for r in results)
    assert statuses == [200, 200, 503, 503, 503, 503], statuses
    for result in results:
        if result.status == 503:
            assert result.headers["x-throttle-prospective-refusal"] == "1"
            assert result.headers["retry-after"]
    assert config.state["inflight"] == config.state["queued"] == 0, "no slot left behind"


def test_local_refusal_flags_are_the_retry_and_aimd_guard():
    """503 IS in THROTTLE_STATUSES: only these flags keep our own refusal out of
    the pushback-retry loop and out of AIMD (the provenance the meter and
    prospective refusals already rely on, reused by the budget path)."""
    import inspect

    assert 503 in config.THROTTLE_STATUSES
    recover = inspect.getsource(proxy._forward_or_recover)
    assert "not (attempt.local_meter_refused or attempt.local_prospective_refused)" in recover
    fast_fail = inspect.getsource(proxy._maybe_fast_fail_throttle_direct)
    assert "attempt.local_prospective_refused" in fast_fail

    declare(models=f"{MODEL}=1/600")
    free_budget.admit_and_count(MODEL, now=10.0)
    decision = free_budget.admit_and_count(MODEL, now=11.0)
    assert decision.allowed is False
    response = decision.response
    assert response.status == 503
    assert response.headers.get("retry-after")


def test_generation_only_gate_leaves_probes_alone():
    assert proxy._retry_after_blocks_path("v1/chat/completions") is True
    assert proxy._retry_after_blocks_path("healthz") is False
    assert proxy._retry_after_blocks_path("status") is False


def test_snapshot_publishes_counts_without_secrets():
    declare(models=f"{MODEL}=3/600")
    free_budget.admit_and_count(MODEL, now=10.0)
    view = free_budget.snapshot(now=11.0)
    assert view["enabled"] is True
    assert view["lane"] == "go"
    assert view["models"][MODEL]["used"] == 1
    assert view["models"][MODEL]["budget"] == 3


# Guard: the wiring must not regress silently if the gate is refactored.
def test_wiring_hooks_are_present_in_the_dispatch_path():
    import inspect

    assert "_free_budget.admit(" in inspect.getsource(proxy._pre_dispatch_gate)
    forward = inspect.getsource(proxy._try_forward)
    assert "_free_budget.admit_and_count(" in forward, "pre-transport claim missing"
    assert "attempt.local_prospective_refused = True" in forward
    assert "_free_budget.budget_blocks()" in inspect.getsource(proxy._pushback_evidence)
