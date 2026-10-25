"""Minute meter recorder: persistence + the same-reset recalibration detector."""

import asyncio
import json

from anthropic_throttle_proxy import meter_recorder as mr

RESET_7D = 1793394000  # the 7d reset epoch both 24/10 and 25/10 readings pointed at


def _bearer(util_5h, util_7d, *, reset_5h=1792894800, reset_7d=RESET_7D, at=1.0):
    return {
        "unified": {
            "util_5h": util_5h,
            "reset_5h": reset_5h,
            "util_7d": util_7d,
            "reset_7d": reset_7d,
            "status": "allowed",
        },
        "unified_at": at,
    }


def _rec(tmp_path, state):
    return mr.MeterRecorder(tmp_path / "meter.jsonl", lambda: state)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_observed_24_25_october_fall_is_a_recalibration_not_consumption(tmp_path):
    # The incident this module exists for: 7d 0.61 -> 0.05, reset epoch unchanged.
    state = {"666a53af": _bearer(0.19, 0.61, at=1792877122.0)}
    rec = _rec(tmp_path, state)
    rec.write(rec.rows(1792877122))
    state["666a53af"] = _bearer(0.30, 0.05, at=1792950000.0)
    row = rec.rows(1792950000)[0]
    assert row["event_7d"] == "recalibration"
    assert row["delta_7d"] == -0.56
    assert row["prev_util_7d"] == 0.61
    assert rec.recalibrations == 1


def test_a_new_reset_epoch_is_a_roll_even_when_util_falls(tmp_path):
    state = {"b": _bearer(0.99, 0.58, reset_5h=100, at=1.0)}
    rec = _rec(tmp_path, state)
    rec.rows(10)
    state["b"] = _bearer(0.19, 0.58, reset_5h=200, at=2.0)
    row = rec.rows(20)[0]
    assert row["event_5h"] == "rolled"
    assert row["prev_reset_5h"] == 100
    assert row["event_7d"] == "steady"
    assert rec.recalibrations == 0


def test_rise_is_consumption_and_rounding_is_steady(tmp_path):
    state = {"b": _bearer(0.50, 0.57, at=1.0)}
    rec = _rec(tmp_path, state)
    assert rec.rows(10)[0]["event_7d"] == "first"
    state["b"] = _bearer(0.60, 0.57 - 0.005, at=2.0)  # within one rounding step
    row = rec.rows(20)[0]
    assert row["event_5h"] == "consumption"
    assert row["delta_5h"] == 0.1
    assert row["event_7d"] == "steady"


def test_no_new_header_is_persisted_as_stale_and_never_classified(tmp_path):
    state = {"b": _bearer(0.50, 0.60, at=1.0)}
    rec = _rec(tmp_path, state)
    rec.write(rec.rows(10))
    rec.write(rec.rows(70))  # a minute later, same unified_at
    first, second = _lines(tmp_path / "meter.jsonl")
    assert "stale" not in first
    assert second["stale"] is True
    assert "event_7d" not in second
    assert second["sample_age_s"] == 69.0


def test_restart_reseeds_from_the_log_so_a_fall_across_it_is_still_caught(tmp_path):
    path = tmp_path / "meter.jsonl"
    before = _rec(tmp_path, {"b": _bearer(0.2, 0.61, at=1.0)})
    before.write(before.rows(10))
    path.write_text(path.read_text() + "{torn line\n")  # crash mid-write must not break seeding

    after = mr.MeterRecorder(path, lambda: {"b": _bearer(0.2, 0.05, at=2.0)})
    after.seed()
    assert after.rows(20)[0]["event_7d"] == "recalibration"


def test_bearers_without_a_7d_sample_are_skipped(tmp_path):
    rec = _rec(tmp_path, {"_anon": {}, "api-key": {"unified": {"util_7d": None}}})
    assert rec.rows(1) == []


def test_env_disables_or_redirects_the_log(monkeypatch, tmp_path):
    monkeypatch.setenv("THROTTLE_METER_LOG", "off")
    assert mr.default_path() is None
    monkeypatch.setenv("THROTTLE_METER_LOG", str(tmp_path / "x.jsonl"))
    assert mr.default_path() == tmp_path / "x.jsonl"
    monkeypatch.delenv("THROTTLE_METER_LOG")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert mr.default_path() == tmp_path / "anthropic-throttle-proxy" / "quota-meter.jsonl"


def test_oversized_log_rotates_to_one_generation(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "MAX_BYTES", 10)
    path = tmp_path / "meter.jsonl"
    path.write_text("x" * 50)
    rec = _rec(tmp_path, {"b": _bearer(0.1, 0.1)})
    rec.write(rec.rows(1))
    assert (tmp_path / "meter.jsonl.1").read_text() == "x" * 50
    assert len(_lines(path)) == 1


async def test_cleanup_context_writes_immediately_and_stops_cleanly(monkeypatch, tmp_path):
    path = tmp_path / "meter.jsonl"
    monkeypatch.setenv("THROTTLE_METER_LOG", str(path))
    ctx = mr.meter_recorder_context(lambda: {"b": _bearer(0.4, 0.6)})(None)
    await ctx.__anext__()  # startup: seeds, starts the loop, which ticks at once
    for _ in range(50):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    try:
        await ctx.__anext__()  # shutdown
    except StopAsyncIteration:
        pass
    assert _lines(path)[0]["util_7d"] == 0.6


async def test_disabled_context_starts_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("THROTTLE_METER_LOG", "off")
    ctx = mr.meter_recorder_context(lambda: {"b": _bearer(0.4, 0.6)})(None)
    await ctx.__anext__()
    try:
        await ctx.__anext__()
    except StopAsyncIteration:
        pass
    assert list(tmp_path.glob("**/*.jsonl")) == []
