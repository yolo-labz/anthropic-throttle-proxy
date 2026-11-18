"""Real held-dispatch/probe seam, with synthetic loopback upstream only."""

import asyncio
import json

import pytest
from test_proxy_app import STREAM_BODY, _stream_setup
from test_proxy_app import client as client

from anthropic_throttle_proxy import config, proxy


async def test_held_success_settles_probe_at_upstream_headers(client, monkeypatch):
    _stream_setup(monkeypatch, hold_knob=True)
    auth = "Bearer HELD-PROBE-B"
    bid = proxy._bearer_id({"Authorization": auth})
    response = await client.post("/v1/messages", data=STREAM_BODY, headers={"Authorization": auth})
    try:
        assert response.status == 200
        body = await asyncio.wait_for(response.read(), 2)
        assert body.count(b"event: message_start") == 1
        lim = config.bearer_limiters[bid]
        print(
            "successful-held-probe",
            json.dumps(
                {
                    "required": lim.retry_probe_required(),
                    "inflight": lim.retry_probe_inflight(),
                    "upstream_sends": sum(client.probe_state["attempts"].values()),
                }
            ),
        )
        assert not lim.retry_probe_required(), "real upstream 2xx did not settle held probe"
        assert not lim.retry_probe_inflight()
        assert client.probe_state["attempts"] == {auth: 1}
    finally:
        response.close()


@pytest.mark.parametrize("warmup", [False, True])
@pytest.mark.parametrize("outcome", ["serve", "cancel", "deadline"])
async def test_overlapping_held_probe_wait_is_observable(client, monkeypatch, warmup, outcome):
    _stream_setup(monkeypatch, hold_knob=True)
    monkeypatch.setattr(client.server.runner.server, "handler_cancellation", False)
    auth = "Bearer OVERLAP-PROBE-B"
    headers = {"Authorization": auth}
    if warmup:
        async with client.post("/v1/messages", data=STREAM_BODY, headers=headers) as response:
            assert b"event: message_start" in await response.read()
    holder = await client.post(
        "/v1/messages",
        data=STREAM_BODY,
        headers={**headers, "X-Stub-Mode": "probe-block-headers"},
    )
    await asyncio.wait_for(client.probe_state["b_started"].wait(), 2)
    lim = config.bearer_limiters[proxy._bearer_id(headers)]
    sends = 1 + int(warmup)
    if outcome == "deadline":
        headers[config.WAIT_BUDGET_HEADER] = "750"
    pending = asyncio.ensure_future(client.post("/v1/messages", data=STREAM_BODY, headers=headers))
    sibling = None
    try:
        print(
            "before-sibling",
            json.dumps(
                {
                    "warmup": warmup,
                    "required": lim.retry_probe_required(),
                    "probe_inflight": lim.retry_probe_inflight(),
                    "upstream_sends": sum(client.probe_state["attempts"].values()),
                }
            ),
        )
        sibling = await asyncio.wait_for(asyncio.shield(pending), 1.5)
        assert sibling.status == 200
        assert sibling.headers["Content-Type"].startswith("text/event-stream")
        if outcome != "deadline":
            prefix = await asyncio.wait_for(sibling.content.readuntil(b"event: ping\n"), 2)
            payload = json.loads((await sibling.content.readline()).removeprefix(b"data: "))
            assert payload["throttler_queue"]["state"] == "waiting"
            assert b"message_start" not in prefix
        assert client.probe_state["attempts"] == {auth: sends}
        if not warmup:
            assert lim.retry_probe_required() and lim.retry_probe_inflight()
        if outcome == "deadline":
            body = await asyncio.wait_for(sibling.read(), 2)
            assert b"event: error" in body and b"event: message_start" not in body
        if outcome != "serve":
            sibling.close()
            for _ in range(200):
                if config.state["admitted_holds"] == 1:
                    break
                await asyncio.sleep(0.01)
            assert config.state["admitted_holds"] == 1
            assert lim.snapshot()["inflight"] == 1
            assert client.probe_state["attempts"] == {auth: sends}
        client.probe_state["release_b_headers"].set()
        assert (await asyncio.wait_for(holder.read(), 2)).count(b"event: message_start") == 1
        if outcome == "serve":
            assert (await asyncio.wait_for(sibling.read(), 2)).count(b"event: message_start") == 1
        for _ in range(200):
            if not config.state["admitted_holds"] and not lim.snapshot()["inflight"]:
                break
            await asyncio.sleep(0.01)
        assert config.state["admitted_holds"] == 0
        assert lim.snapshot()["inflight"] == 0
        assert client.probe_state["attempts"] == {auth: sends + int(outcome == "serve")}
        assert not lim.retry_probe_required()
        assert not lim.retry_probe_inflight()
        assert not lim.snapshot()["queued_total"]
        print("completed-upstream-sends", sends + int(outcome == "serve"))
    finally:
        client.probe_state["release_b_headers"].set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        if sibling is not None:
            sibling.close()
        holder.close()
