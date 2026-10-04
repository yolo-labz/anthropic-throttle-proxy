"""Admission-closure surface: the authoritative verdict reflects the quiesce gate.

The HANDLER gate (PR #298) ENFORCES closure at request level. This slice folds
the flag into the `/__throttle/admission` verdict — which the ingress endpoint
reader, routing URLs and the shared limiter predicate consume — so those
consumers REFLECT closure; they are NOT universal producer coverage. Health
publishes `admission_closed`, making closure affirmatively observable (never
inferred from zero counts).

Reuses the shared `meter_rows` fixture (conftest) and `test_admission._bound_bearers`;
handlers invoked directly (`main()` owns route registration). Pure logic only.
"""

from __future__ import annotations

import json

import pytest
from test_admission import _bound_bearers

from anthropic_throttle_proxy import config, proxy


@pytest.fixture(autouse=True)
def _isolate_state():
    """Process-global registries leak across modules; isolate IN and OUT
    (the trusted-display projection counts the visible bearer set)."""
    saved_state = dict(config.bearer_state)
    saved_limiters = dict(config.bearer_limiters)
    saved_closed = proxy._admission_closed
    config.bearer_state.clear()
    config.bearer_limiters.clear()
    proxy._admission_closed = False
    yield
    config.bearer_state.clear()
    config.bearer_state.update(saved_state)
    config.bearer_limiters.clear()
    config.bearer_limiters.update(saved_limiters)
    proxy._admission_closed = saved_closed


def _open_gate() -> None:
    proxy._admission_closed = False


async def _verdict() -> dict:
    return json.loads((await proxy.admission(None)).body)


async def _health() -> dict:
    return json.loads((await proxy.health(None)).body)


async def test_quiesce_flips_the_authoritative_verdict(tmp_path, monkeypatch, meter_rows):
    _bound_bearers(tmp_path, monkeypatch, meter_rows())
    _open_gate()
    before = await _verdict()
    assert before["allow"] is True
    assert before["state"] != "quiesced"

    await proxy.quiesce(None)
    closed = await _verdict()
    assert closed["allow"] is False
    assert closed["state"] == "quiesced"
    assert "quiesce" in closed["reason"]

    await proxy.resume(None)
    after = await _verdict()
    assert after["allow"] is True
    assert after["state"] != "quiesced"


async def test_health_reports_admission_closed_state(tmp_path, monkeypatch, meter_rows):
    _bound_bearers(tmp_path, monkeypatch, meter_rows())
    _open_gate()
    assert (await _health())["admission_closed"] is False
    await proxy.quiesce(None)
    body = await _health()
    assert body["admission_closed"] is True
    assert body["admitted_holds"] == 0, "zero counts do NOT imply open or closed"
    await proxy.resume(None)
    assert (await _health())["admission_closed"] is False


async def test_closed_state_survives_zero_counts(tmp_path, monkeypatch, meter_rows):
    """Falsifier: every counter at zero must not read as an open gate."""
    _bound_bearers(tmp_path, monkeypatch, meter_rows())
    _open_gate()
    config.state.update(
        {"inflight": 0, "queued": 0, "keepalive_holds_active": 0, "admitted_holds": 0}
    )
    await proxy.quiesce(None)
    verdict = await _verdict()
    assert verdict["allow"] is False
    assert config.state["inflight"] == 0 and config.state["admitted_holds"] == 0
    await proxy.resume(None)


async def test_quiesce_resume_idempotent_on_verdict(tmp_path, monkeypatch, meter_rows):
    _bound_bearers(tmp_path, monkeypatch, meter_rows())
    _open_gate()
    for _ in range(2):
        await proxy.quiesce(None)
    assert (await _verdict())["allow"] is False
    for _ in range(2):
        await proxy.resume(None)
    assert (await _verdict())["allow"] is True
