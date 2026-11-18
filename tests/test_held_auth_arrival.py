"""Auth arrival and cleanup through the actual producer, independent of native Pi."""

import asyncio
import inspect
import json
import os
from pathlib import Path

import pytest
from aiohttp import web
from test_keepalive_hold import _SSE_BODY, _make_client_with_upstream

from anthropic_throttle_proxy import config, proxy


async def settled(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("owned producer state did not settle within three seconds")


@pytest.mark.parametrize(
    "history,fresh,held",
    [
        (False, False, True),
        (False, True, True),
        (True, False, True),
        (True, True, True),
        (False, True, False),
    ],
)
async def test_auth_arrival_and_envelope(monkeypatch, history, fresh, held):
    trace = []
    arrivals = []
    release = asyncio.Event()
    before = "Bearer SYNTHETIC-PJ-BEFORE"
    auth = "Bearer SYNTHETIC-PJ-FRESH" if fresh else before
    bid = proxy._bearer_id({"Authorization": auth})

    def snapshot():
        return {
            "bearers": {b: lim.snapshot() for b, lim in config.bearer_limiters.items()},
            "state": {
                k: config.state.get(k, 0)
                for k in ("inflight", "queued", "admitted_holds", "keepalive_holds_active")
            },
        }

    async def upstream(request):
        # Arrival is recorded BEFORE label routing, body parsing or auth refusal.
        entry = {"index": len(arrivals), "path": request.path, "state": snapshot()}
        arrivals.append(entry)
        label = request.headers["X-PJ-Case"]
        entry["label"] = label
        await request.read()
        if label == "holder":
            await release.wait()
        if label == "auth":
            entry["sent_status"] = 401
            return web.json_response(
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "synthetic invalid credential",
                    },
                },
                status=401,
            )
        entry["sent_status"] = 200
        return web.Response(body=_SSE_BODY, content_type="text/event-stream")

    actual_slot = proxy._run_slot

    async def slot(**kwargs):
        request = kwargs["request"]
        trace.append(
            {
                "event": "slot-entry",
                "label": request.headers.get("X-PJ-Case"),
                "deadline": kwargs["wait_deadline"],
                "lease": dict(kwargs["probe_lease"]),
                "state": snapshot(),
            }
        )
        try:
            return await actual_slot(**kwargs)
        finally:
            trace.append(
                {
                    "event": "slot-exit",
                    "label": request.headers.get("X-PJ-Case"),
                    "deadline": kwargs["wait_deadline"],
                    "lease": dict(kwargs["probe_lease"]),
                    "state": snapshot(),
                }
            )

    monkeypatch.setattr(proxy, "_run_slot", slot)
    actual_emit = proxy._emit_sse_error_terminal

    async def emit(response, message, error_type="throttle_timeout"):
        trace.append(
            {
                "event": "terminal",
                "caller": inspect.currentframe().f_back.f_code.co_name,
                "type": error_type,
                "message": message,
                "state": snapshot(),
            }
        )
        return await actual_emit(response, message, error_type)

    monkeypatch.setattr(proxy, "_emit_sse_error_terminal", emit)
    actual_verdict = proxy._keepalive_reclassified_terminal

    async def verdict(**kwargs):
        trace.append(
            {
                "event": "reclassified",
                "status": kwargs["status"],
                "upstream_body": bytes(kwargs["attempt"].captured or b"").decode(),
                "state": snapshot(),
            }
        )
        return await actual_verdict(**kwargs)

    monkeypatch.setattr(proxy, "_keepalive_reclassified_terminal", verdict)
    app = web.Application()
    app.router.add_post("/v1/messages", upstream)
    client, server = await _make_client_with_upstream(monkeypatch, app)
    monkeypatch.setattr(config, "QUEUE_MODE", "fair")
    monkeypatch.setattr(config, "STREAM_HOLD_ON_DEFER", held)
    monkeypatch.setattr(config, "ACCOUNT_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "API_KEY_ROUTING_MODE", "off")
    monkeypatch.setattr(config, "PRIORITY_RESERVE_SLOTS", 0)
    monkeypatch.setattr(client.server.runner.server, "handler_cancellation", False)
    config.state["admitted_holds"] = 0
    responses = []

    async def post(label, bearer=before, budget=None):
        headers = {"Authorization": bearer, "X-PJ-Case": label}
        if budget is not None:
            headers[config.WAIT_BUDGET_HEADER] = str(budget)
        response = await client.post(
            "/v1/messages",
            json={"model": "synthetic-auth", "stream": True, "messages": []},
            headers=headers,
        )
        responses.append(response)
        return response

    def clean():
        s = snapshot()
        return not any(s["state"].values()) and all(
            not b["inflight"] and not b["queued_total"] and not b["retry_probe_inflight"]
            for b in s["bearers"].values()
        )

    try:
        if not fresh or history:
            assert b"message_stop" in await (await post("warmup")).read()
        if history:
            holder = await post("holder")
            await settled(lambda: any(a.get("label") == "holder" for a in arrivals))
            cancelled = await post("cancel")
            cancelled.close()
            await settled(lambda: config.state["admitted_holds"] == 1)
            deadline = await post("deadline", budget=100)
            assert b"throttle_timeout" in await deadline.read()
            await settled(lambda: config.state["admitted_holds"] == 1)
            aborted = await post("signal-abort")
            aborted.close()
            await settled(lambda: config.state["admitted_holds"] == 1)
            assert not any(
                a.get("label") in {"cancel", "deadline", "signal-abort"} for a in arrivals
            )
            release.set()
            assert b"message_stop" in await holder.read()
            await settled(clean)
        trace.append({"event": "before-auth", "fresh": fresh, "state": snapshot()})
        response = await post("auth", bearer=auth)
        body = await response.read()
        await settled(clean)
        auth_arrivals = [a for a in arrivals if a.get("label") == "auth"]
        trace.append(
            {
                "event": "auth-result",
                "status": response.status,
                "body": body.decode(),
                "arrivals": len(auth_arrivals),
                "state": snapshot(),
            }
        )
        assert len(auth_arrivals) == 1 and auth_arrivals[0]["sent_status"] == 401
        assert not config.bearer_limiters[bid].retry_probe_inflight()
        if held:
            errors = [
                json.loads(line[6:])["error"]
                for line in body.splitlines()
                if line.startswith(b"data: ") and json.loads(line[6:]).get("type") == "error"
            ]
            assert len(errors) == 1
            assert errors[0] == {
                "type": "authentication_error",
                "message": "synthetic invalid credential",
            }
            assert b"event: message_start" not in body
        else:
            assert response.status == 401
            assert json.loads(body)["error"]["type"] == "authentication_error"
    finally:
        release.set()
        for response in responses:
            response.close()
        await client.close()
        await server.close()
        directory = os.environ.get("PJ_AUTH_EVIDENCE")
        if directory:
            await asyncio.to_thread(Path(directory).mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                Path(directory, f"history-{history}-fresh-{fresh}-held-{held}.json").write_text,
                json.dumps(
                    {
                        "producer": proxy.__file__,
                        "arrivals": arrivals,
                        "trace": trace,
                        "final": snapshot(),
                    },
                    indent=2,
                )
                + "\n",
            )
