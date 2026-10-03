"""Spec245 STRICT adapter for spec244's six budget failures, loopback only.

Requires the coherent runtime + producer + dispatch/refusal integration. The
retained desktop baseline lacks that wiring and MUST NOT pass via a fixture
admission shim. Only configured identity, clocks and HTTP test endpoints are
substituted; handler, forwarding, limiter, retries, runtime and ledger are real.
The original off oracle and its exact-six-failures evidence check stay unchanged.
"""

import asyncio
import importlib.util
import json
from collections import Counter
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, forwarding, pacing, proxy
from anthropic_throttle_proxy import prospective_refusal as refusal_module
from anthropic_throttle_proxy import prospective_runtime as runtime_module
from anthropic_throttle_proxy.ledger import WINDOW_S, Budgets
from anthropic_throttle_proxy.prospective import account_request
from anthropic_throttle_proxy.prospective_admission import PersistenceOwner, Scope
from anthropic_throttle_proxy.prospective_refusal import ProspectiveRefusal, RefusalReason
from anthropic_throttle_proxy.prospective_runtime import (
    RUNTIME_KEY,
    ProspectiveRuntime,
)
from anthropic_throttle_proxy.prospective_scope import ScopeResolver


def _load_original_oracle():
    path = (
        Path(__file__).resolve().parents[1] / "specs/244-prospective-admission/check_admission.py"
    )
    spec = importlib.util.spec_from_file_location("spec244_admission_oracle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ORIGINAL = _load_original_oracle()
burst = _ORIGINAL.burst  # unmodified fixture for the three ORIGINAL off controls
PATHS = _ORIGINAL.PATHS
KEYS = _ORIGINAL.KEYS
within_budget = _ORIGINAL.within_budget
SOURCE = "fixture-configured-source"
MODEL = "fixture-model"
SCOPE_KEY = ("fixture-upstream", "account-one", MODEL)
PUBLIC_BUDGET = "spec244-fixture"
RETRY_AFTER = 3
DONE = b"data: [DONE]\n\n"

# Deliberately separate experiments, not provider defaults. A supported input
# plus its unchanged output bound of 60 must fit <=100 units in ALL experiments.
TOKEN_BUDGET = Budgets(2, 100)
COMPLETED_BUDGET = Budgets(2, 4 * 100)  # token room for all four attempted requests
RETRY_BUDGET = Budgets(1, 2 * 100)  # token room for both attempts; RPM alone binds


def _payload(path):
    output_key = "max_output_tokens" if path.endswith("/responses") else "max_tokens"
    payload = {"model": MODEL, "stream": True, output_key: 60}
    if path.endswith("/responses"):
        payload["input"] = "synthetic input"
    else:
        payload["messages"] = [{"role": "user", "content": "synthetic input"}]
    return payload


def _runtime(endpoint, state_path, budgets):
    resolver = ScopeResolver(
        [
            {
                "source": SOURCE,
                "endpoint": endpoint,
                "alias": MODEL,
                "upstream": SCOPE_KEY[0],
                "account": SCOPE_KEY[1],
                "model": MODEL,
                "budgets": budgets,
            }
        ],
        sources=[SOURCE],
        endpoints=[endpoint],
        models=[MODEL],
    )
    return ProspectiveRuntime(
        mode="strict",
        resolver=resolver,
        scopes=[Scope(SCOPE_KEY, budgets, str(state_path), True)],
        max_pending=4,
        wait_timeout=5,
        budget_label=PUBLIC_BUDGET,
        retry_after_s=RETRY_AFTER,
    )


def _bind_fixture_authority(monkeypatch, endpoint):
    """Both declared fixture keys belong to one operator-specified account.

    Replace credential discovery, not dispatch metadata: real initial routing,
    post-wait rerouting and half-open probe ownership still execute.
    """
    assert config.UPSTREAM == endpoint
    monkeypatch.setattr(
        proxy,
        "_route_account_if_enabled",
        lambda headers, incoming_bid, **kwargs: (incoming_bid, SOURCE),
    )


async def _waves(client, path, keys, waves, release):
    results = []
    for _ in range(waves):
        release.clear()
        async with asyncio.timeout(10):
            responses = await asyncio.gather(
                *(
                    client.post(path, json=_payload(path), headers={"Authorization": key})
                    for key in keys
                )
            )
            # Admitted streams are held; denied requests must already be LOCAL
            # 503s, not queued forever waiting for the admitted stream to finish.
            release.set()
            bodies = await asyncio.gather(*(response.read() for response in responses))
        results.append(
            [
                {"status": response.status, "headers": response.headers, "body": body}
                for response, body in zip(responses, bodies, strict=True)
            ]
        )
    return results


@pytest.fixture
async def strict_burst(monkeypatch, proxy_admission_state, tmp_path):
    clock = SimpleNamespace(now=0.0)
    ports, upstream_ports = set(), set()
    paced_times, wire = [], []
    original_request = aiohttp.ClientSession._request

    async def loopback_only(self, method, url, **kwargs):
        target = urlsplit(str(url))
        assert target.hostname == "127.0.0.1" and target.port in ports, "non-fixture egress refused"
        if target.port in upstream_ports:
            assert method == "POST" and target.path in PATHS
            body = kwargs.get("data")
            assert isinstance(body, bytes), "capture the actual final transport body"
            wire.append({"at": clock.now, "body": body})
        return await original_request(self, method, url, **kwargs)

    async def advance(delay):
        clock.now += delay
        await asyncio.sleep(0)

    async def paced():
        await pacing._pace_dispatch()
        paced_times.append(clock.now)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", loopback_only)
    monkeypatch.setattr(pacing, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(pacing, "asyncio", SimpleNamespace(sleep=advance))
    monkeypatch.setattr(pacing, "_last_dispatch_ts", -8.0)
    monkeypatch.setattr(forwarding, "_pace_dispatch", paced)
    # Actual PersistenceOwner, with its existing clock-injection API. No fake
    # reserve/lease/pool/save, and no global deadline/server-clock patching.
    monkeypatch.setattr(
        runtime_module, "PersistenceOwner", partial(PersistenceOwner, clock=lambda: clock.now)
    )
    _ORIGINAL.configure_fixture_proxy(monkeypatch)

    async def run(path, keys, *, budgets=TOKEN_BUDGET, waves=1, first_429=False):
        provider = _ORIGINAL.FixtureProvider(first_429)
        seen, release = provider.seen, provider.release

        remote = web.Application()
        remote.router.add_post(path, provider.handle)
        state_path = tmp_path / "strict-ledger.json"
        async with TestServer(remote) as origin:
            ports.add(origin.port)
            upstream_ports.add(origin.port)
            endpoint = str(origin.make_url("")).rstrip("/")
            monkeypatch.setattr(config, "UPSTREAM", endpoint)
            monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", int(first_429))
            _bind_fixture_authority(monkeypatch, endpoint)
            runtime = _runtime(endpoint, state_path, budgets)
            app = web.Application()
            app[RUNTIME_KEY] = runtime
            app.on_response_prepare.append(forwarding.stamp_proxy_marker)
            app.router.add_route("*", "/{path:.*}", proxy.handler)
            try:
                await runtime.start()
                assert isinstance(runtime._owner, PersistenceOwner)
                async with TestClient(TestServer(app)) as client:
                    ports.add(client.server.port)
                    try:
                        responses = await _waves(client, path, keys, waves, release)
                    finally:
                        release.set()
            finally:
                release.set()
                await runtime.aclose()
        assert config.state["inflight"] == config.state["queued"] == 0
        assert provider.active == 0
        assert len(wire) == len(seen)
        assert clock.now < WINDOW_S, "these experiments must stay inside ONE rolling window"
        assert all(b - a >= 8 for a, b in zip(paced_times, paced_times[1:], strict=False))
        return {
            "attempts": seen,
            "wire": wire,
            "waves": responses,
            "paced_times": paced_times,
            "peak": provider.peak,
            "budgets": budgets,
            "ledger": json.loads(state_path.read_text()) if state_path.exists() else None,
        }

    yield run


def _assert_outcomes(receipt, *, admitted, denied, reason=RefusalReason.EXHAUSTED):
    responses = [response for wave in receipt["waves"] for response in wave]
    assert Counter(response["status"] for response in responses) == Counter(
        {200: admitted, 503: denied}
    )
    expected = ProspectiveRefusal(reason, RETRY_AFTER, PUBLIC_BUDGET)
    for response in responses:
        headers = response["headers"]
        if response["status"] == 200:
            assert response["body"] == DONE
            assert not any(name in headers for name in refusal_module.PROVENANCE_HEADERS)
            continue
        assert json.loads(response["body"]) == expected.error_payload()
        assert headers["Retry-After"] == str(RETRY_AFTER)
        for name, value in refusal_module.PROVENANCE_HEADERS.items():
            assert headers[name] == value
        assert headers[refusal_module.PROVENANCE_BUDGET_HEADER] == PUBLIC_BUDGET
        assert headers[refusal_module.PROVENANCE_REASON_HEADER] == reason.value


def _assert_budget_and_durable_debt(receipt):
    budgets = receipt["budgets"]  # EXACT same object supplied to resolver and owner
    costs = [account_request(attempt["body"], {}) for attempt in receipt["wire"]]
    assert all(cost is not None and cost.output_bound == 60 for cost in costs)
    reservations = [cost.input_tokens + cost.output_bound for cost in costs]
    assert all(cost <= 100 for cost in reservations), "small-request control must remain small"
    assert all(0 <= attempt["at"] < WINDOW_S for attempt in receipt["wire"])
    assert all(attempt["output_bound"] == 60 for attempt in receipt["attempts"])
    assert all(attempt["model"] == MODEL for attempt in receipt["attempts"])
    assert all(attempt["account"] == SCOPE_KEY[1] for attempt in receipt["attempts"])
    within_budget(sum(reservations), budgets.max_tokens, "input + output reservation")
    within_budget(sum(x["output_bound"] for x in receipt["attempts"]), budgets.max_tokens, "output")
    within_budget(len(receipt["attempts"]), budgets.max_requests, "requests inside 60s")
    within_budget(receipt["peak"], 2, "account concurrency")
    ledger = receipt["ledger"]
    if not receipt["attempts"]:
        assert ledger is None, "unsupported requests must not allocate persisted debt"
        return
    assert ledger is not None and len(ledger["lanes"]) == 1
    lane = ledger["lanes"][0]
    assert lane["key"] == list(SCOPE_KEY)
    entries = lane["entries"]
    assert len(entries) == len(receipt["attempts"])
    assert Counter(entry["tokens"] for entry in entries) == Counter(reservations)
    assert all(0 <= entry["at"] < WINDOW_S and not entry["settled"] for entry in entries)


@pytest.mark.parametrize("path", PATHS)
async def test_original_default_off_positive_controls_are_unchanged(burst, path):
    await _ORIGINAL.test_small_fixture_request_stays_within_all_budgets(burst, path)


@pytest.mark.parametrize("path", PATHS[:2])
async def test_strict_supported_small_request_is_not_vacuously_refused(strict_burst, path):
    receipt = await strict_burst(path, ["Bearer fixture-key-a"])
    _assert_outcomes(receipt, admitted=1, denied=0)
    assert len(receipt["attempts"]) == receipt["peak"] == 1
    _assert_budget_and_durable_debt(receipt)


@pytest.mark.parametrize("path", PATHS)
async def test_two_slots_do_not_oversubscribe_fixture_token_window(strict_burst, path):
    receipt = await strict_burst(path, ["Bearer fixture-key-a"] * 2)
    unsupported = path.endswith("/responses")
    admitted = 0 if unsupported else 1
    reason = RefusalReason.UNKNOWN if unsupported else RefusalReason.EXHAUSTED
    _assert_outcomes(receipt, admitted=admitted, denied=2 - admitted, reason=reason)
    assert len(receipt["attempts"]) == receipt["peak"] == admitted
    assert receipt["paced_times"] == [0, 8]
    _assert_budget_and_durable_debt(receipt)


async def test_two_keys_share_fixture_account_capacity(strict_burst):
    receipt = await strict_burst(PATHS[1], list(KEYS) * 2)
    _assert_outcomes(receipt, admitted=1, denied=3)
    # 100 tokens binds before the two-stream ceiling: do NOT require peak==2.
    assert len(receipt["attempts"]) == receipt["peak"] == 1
    assert receipt["paced_times"] == [0, 8, 16, 24]
    _assert_budget_and_durable_debt(receipt)


async def test_completed_requests_still_count_in_fixture_request_window(strict_burst):
    receipt = await strict_burst(
        PATHS[1], ["Bearer fixture-key-a"] * 2, budgets=COMPLETED_BUDGET, waves=2
    )
    _assert_outcomes(receipt, admitted=2, denied=2)
    assert [[response["status"] for response in wave] for wave in receipt["waves"]] == [
        [200, 200],
        [503, 503],
    ]
    assert len(receipt["attempts"]) == 2
    assert receipt["paced_times"] == [0, 8, 16, 24]
    _assert_budget_and_durable_debt(receipt)


async def test_retry_consumes_another_fixture_request_allocation(strict_burst):
    receipt = await strict_burst(
        PATHS[1], ["Bearer fixture-key-a"], budgets=RETRY_BUDGET, first_429=True
    )
    _assert_outcomes(receipt, admitted=0, denied=1)
    # The original 429 is real spend; the prospective retry is a LOCAL denial.
    assert len(receipt["attempts"]) == 1
    assert receipt["paced_times"] == [0, 8]
    _assert_budget_and_durable_debt(receipt)
