"""Admission-closure surface: the authoritative verdict honors the quiesce gate.

The `/__throttle/admission` verdict is what the routing/ingress/limiter dispatch
layers consult before admitting work, so folding the gate into it blocks NEW
requests at request level (keepalive/pipelined/relayed arrivals) while admitted
streams continue — and `admission_closed` in health makes closure affirmatively
observable (never inferred from zero counts).

Reuses the shared `meter_rows` fixture (conftest) and `test_admission._bound_bearers`;
handlers invoked directly (`main()` owns route registration). Pure logic only.
"""

from __future__ import annotations

import json

from test_admission import _bound_bearers

from anthropic_throttle_proxy import config, proxy


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
