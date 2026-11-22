"""A quiesce closes admission for a bounded lifetime, then reopens by itself.

11/11/2026 15:51 BRT: a seat POSTed /__throttle/quiesce and died before resume;
481 refusals in 4 min 12 s until a human reopened it. Drives the real handler
and control routes (``_scenario`` from test_admission_quiesce).
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from test_admission_quiesce import _BODY, _HEADERS, _refused, _scenario

from anthropic_throttle_proxy import config, proxy


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(proxy, "_quiesce_clock", fake)
    monkeypatch.setattr(config, "QUIESCE_DEFAULT_S", 300.0)
    monkeypatch.setattr(config, "QUIESCE_MAX_S", 900.0)
    monkeypatch.setattr(config, "QUEUE_TIMEOUT_RETRY_AFTER_S", 5)
    return fake


async def _served(client) -> None:
    ok = await client.post("/v1/messages", data=_BODY, headers=_HEADERS)
    await ok.read()
    assert ok.status == 200


async def _health(client) -> dict:
    return await (await client.get("/__throttle/health")).json()


async def test_expiry_reopens_admission_with_no_call(monkeypatch, clock) -> None:
    async with _scenario(monkeypatch) as (client, upstream):
        closed = await client.post("/__throttle/quiesce")
        assert await closed.json() == {"admission": "closed", "expires_in_s": 300.0}
        await _refused(client)
        clock.now += 299.0
        await _refused(client)
        clock.now += 1.0  # lifetime over; nobody calls resume
        await _served(client)
        assert upstream.hits == 1
        health = await _health(client)
        assert health["admission_closed"] is False
        assert health["admission_closed_remaining_s"] is None


async def test_explicit_resume_before_expiry_still_reopens(monkeypatch, clock) -> None:
    async with _scenario(monkeypatch) as (client, _upstream):
        await client.post("/__throttle/quiesce", params={"ttl_s": "600"})
        await _refused(client)
        clock.now += 10.0
        opened = await client.post("/__throttle/resume")
        assert (await opened.json())["admission"] == "open"
        await _served(client)


async def test_lifetime_above_the_maximum_is_clamped(monkeypatch, clock) -> None:
    async with _scenario(monkeypatch) as (client, _upstream):
        closed = await client.post("/__throttle/quiesce", params={"ttl_s": "86400"})
        assert (await closed.json())["expires_in_s"] == 900.0
        clock.now += 900.0
        await _served(client)


async def test_repeated_quiesce_cannot_extend_past_the_maximum(monkeypatch, clock) -> None:
    async with _scenario(monkeypatch) as (client, _upstream):
        await client.post("/__throttle/quiesce")
        for _ in range(3):  # a stuck loop re-quiescing every 5 minutes
            clock.now += 299.0
            again = await client.post("/__throttle/quiesce")
            assert (await again.json())["expires_in_s"] <= 900.0 - (clock.now - 1000.0)
        assert clock.now - 1000.0 == 897.0
        await _refused(client)
        clock.now = 1000.0 + 900.0  # first close + maximum
        await _served(client)


@pytest.mark.parametrize("bad", ["abc", "nan", "inf"])
async def test_invalid_lifetime_is_refused_and_changes_nothing(monkeypatch, clock, bad) -> None:
    async with _scenario(monkeypatch) as (client, _upstream):
        response = await client.post("/__throttle/quiesce", params={"ttl_s": bad})
        assert response.status == 400
        await _served(client)


async def test_retry_after_never_exceeds_the_time_left(monkeypatch, clock) -> None:
    async with _scenario(monkeypatch) as (client, _upstream):
        await client.post("/__throttle/quiesce", params={"ttl_s": "60"})
        assert (await _refused(client)).headers["retry-after"] == "5"  # 60 s left
        clock.now += 57.5  # 2.5 s left
        assert (await _refused(client)).headers["retry-after"] == "3"
        health = await _health(client)
        assert health["admission_closed"] is True
        assert health["admission_closed_remaining_s"] == pytest.approx(2.5)
        clock.now += 2.0  # 0.5 s left: never 0, never past the reopen
        assert (await _refused(client)).headers["retry-after"] == "1"


async def test_a_restarted_gateway_starts_open() -> None:
    """Stated: the gate is process memory only, never persisted, so a restart
    (a fresh process) reopens admission. Checked in a real fresh interpreter."""
    probe = (
        "from anthropic_throttle_proxy import proxy as p;"
        "print(p._admission_gate_closed(), p._admission_remaining_s(), p._admission_deadline)"
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", probe, stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await asyncio.wait_for(child.communicate(), timeout=60)
    assert child.returncode == 0
    assert stdout.decode().split() == ["False", "None", "None"]
