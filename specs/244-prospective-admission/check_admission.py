"""Offline evidence of missing prospective admission, not MiMo entitlement.

This explicitly invoked spec is RED on the current production implementation.
Fictional budgets (100 reservation units, 2 requests/60s) belong only to the fake
provider. No live limit is set. See spec.md for the invocation.
The real handler, fair limiter, pacer, stream forwarding and retry loop run.
"""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, forwarding, pacing, proxy

PATHS = ("/v1/messages", "/v1/chat/completions", "/v1/responses")
# The fixture, not client-supplied account metadata, owns this identity mapping.
KEYS = {"Bearer fixture-key-a": "account-one", "Bearer fixture-key-b": "account-one"}


class BudgetExceeded(AssertionError):
    """Only a measured budget violation may be classified as an expected gap."""


def within_budget(value, limit, label):
    if value > limit:
        raise BudgetExceeded(f"{label}: {value} > fictional budget {limit}")


@pytest.fixture
async def burst(monkeypatch, proxy_admission_state):
    """Hold streams after headers so overlapping reservations are observable."""
    clock = SimpleNamespace(now=0.0)
    sends = []
    ports = set()
    original_request = aiohttp.ClientSession._request

    async def loopback_only(self, method, url, **kwargs):
        target = urlsplit(str(url))
        assert target.hostname == "127.0.0.1" and target.port in ports, "non-fixture egress refused"
        return await original_request(self, method, url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", loopback_only)

    async def advance(delay):
        clock.now += delay
        await asyncio.sleep(0)

    async def paced():
        await pacing._pace_dispatch()
        sends.append(clock.now)

    # Virtualize only the pacer's clock/sleep, not asyncio's server/deadline clock.
    # The live-sized 8s dispatch gap therefore costs no wall-clock sleep.
    monkeypatch.setattr(pacing, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(pacing, "asyncio", SimpleNamespace(sleep=advance))
    monkeypatch.setattr(pacing, "_last_dispatch_ts", -8.0)
    monkeypatch.setattr(forwarding, "_pace_dispatch", paced)
    for key, value in {
        "CENTRAL_URL": "",
        "QUEUE_MODE": "fair",
        "MAX_CONCURRENT": 2,
        "AIMD_INITIAL_CONCURRENT": 2,
        "PRIORITY_RESERVE_SLOTS": 0,
        "RATE_PUSHBACK_RETRIES": 0,
        "AIMD_BACKOFF_S": 0.001,
        "MIN_DISPATCH_GAP_S": 8.0,
        "QUEUE_MAX_WAIT_S": 5,
        "ACCOUNT_ROUTING_MODE": "off",
        "API_KEY_ROUTING_MODE": "off",
        "KEEPALIVE_HOLD": False,
    }.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setenv("THROTTLE_ACCOUNT_ROUTING", "off")

    async def run(path, keys, *, waves=1, first_429=False):
        seen = []
        active = peak = 0
        release = asyncio.Event()

        async def upstream(request):
            nonlocal active, peak
            payload = await request.json()
            seen.append(
                {
                    "account": KEYS[request.headers["Authorization"]],
                    "model": payload["model"],
                    "output_bound": payload.get("max_tokens", payload.get("max_output_tokens")),
                }
            )
            if first_429 and len(seen) == 1:
                return web.json_response({"error": "fixture pushback"}, status=429)
            active += 1
            peak = max(peak, active)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            try:
                await response.prepare(request)
                await release.wait()
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
                return response
            finally:
                active -= 1

        remote = web.Application()
        remote.router.add_post(path, upstream)
        async with TestServer(remote) as origin:
            ports.add(origin.port)
            monkeypatch.setattr(config, "UPSTREAM", str(origin.make_url("")).rstrip("/"))
            monkeypatch.setattr(config, "RATE_PUSHBACK_RETRIES", int(first_429))
            app = web.Application()
            app.on_response_prepare.append(forwarding.stamp_proxy_marker)
            app.router.add_route("*", "/{path:.*}", proxy.handler)
            async with TestClient(TestServer(app)) as client:
                ports.add(client.server.port)
                output_key = "max_output_tokens" if path.endswith("/responses") else "max_tokens"
                payload = {"model": "fixture-model", "stream": True, output_key: 60}
                if path.endswith("/responses"):
                    payload["input"] = "synthetic input"
                else:
                    payload["messages"] = [{"role": "user", "content": "synthetic input"}]
                try:
                    for _ in range(waves):
                        release.clear()
                        async with asyncio.timeout(3):
                            responses = await asyncio.gather(
                                *(
                                    client.post(path, json=payload, headers={"Authorization": k})
                                    for k in keys
                                )
                            )
                            # All admitted streams are open; no final usage has arrived.
                            release.set()
                            bodies = await asyncio.gather(*(r.read() for r in responses))
                        assert [r.status for r in responses] == [200] * len(keys)
                        assert bodies == [b"data: [DONE]\n\n"] * len(keys)
                finally:
                    release.set()
        assert config.state["inflight"] == config.state["queued"] == 0
        assert active == 0
        assert len(sends) == len(seen)
        assert all(b - a >= 8 for a, b in zip(sends, sends[1:], strict=False))
        return {"attempts": seen, "dispatch_times": sends, "peak": peak}

    yield run


@pytest.mark.parametrize("path", PATHS)
async def test_small_fixture_request_stays_within_all_budgets(burst, path):
    receipt = await burst(path, ["Bearer fixture-key-a"])
    within_budget(sum(x["output_bound"] for x in receipt["attempts"]), 100, "output reservation")
    within_budget(receipt["peak"], 2, "account concurrency")
    within_budget(len(receipt["attempts"]), 2, "window requests")


@pytest.mark.parametrize("path", PATHS)
async def test_two_slots_do_not_oversubscribe_fixture_token_window(burst, path):
    receipt = await burst(path, ["Bearer fixture-key-a"] * 2)
    assert receipt["peak"] == 2, "reproducer did not overlap streams"
    assert receipt["dispatch_times"] == [0, 8]
    print(json.dumps(receipt, sort_keys=True))
    # Output bounds alone exceed this fictional budget; input is not even counted.
    within_budget(sum(x["output_bound"] for x in receipt["attempts"]), 100, "output reservation")


async def test_two_keys_share_fixture_account_capacity(burst):
    receipt = await burst(PATHS[1], list(KEYS) * 2)
    assert {x["account"] for x in receipt["attempts"]} == {"account-one"}
    print(json.dumps(receipt, sort_keys=True))
    within_budget(receipt["peak"], 2, "account concurrency")


async def test_completed_requests_still_count_in_fixture_request_window(burst):
    receipt = await burst(PATHS[1], ["Bearer fixture-key-a"] * 2, waves=2)
    assert receipt["peak"] == 2
    assert receipt["dispatch_times"] == [0, 8, 16, 24]
    print(json.dumps(receipt, sort_keys=True))
    within_budget(len(receipt["attempts"]), 2, "requests inside 60s")


async def test_retry_consumes_another_fixture_request_allocation(burst):
    receipt = await burst(PATHS[1], ["Bearer fixture-key-a"], first_429=True)
    assert receipt["dispatch_times"] == [0, 8]
    print(json.dumps(receipt, sort_keys=True))
    within_budget(len(receipt["attempts"]), 1, "attempts including retry")
