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


def test_startup_seed_restores_burn_history_from_the_persisted_log(tmp_path, monkeypatch) -> None:
    """End to end on the 06/11/2026 restart shape: the persisted log (with a torn
    first line and stale repeats) restores the plateau, so one 1 pt step after
    the restart is judged on schedule instead of as a fleet-wide overpace."""
    import json as _json

    from anthropic_throttle_proxy import meter_recorder as mr
    from anthropic_throttle_proxy import proxy
    from anthropic_throttle_proxy import usage_allocator as ua

    reset = 1_794_564_000.0
    restart = reset - ua.WEEK_S + 7.4 * 3600
    now = restart + 55 * 60
    log_path = tmp_path / "quota-meter.jsonl"
    lines = ['{"torn": tr']
    t = restart - 20 * 3600
    while t < restart:
        row = {"bearer": "b144f62f", "sampled_at": t, "util_7d": 0.0, "reset_7d": reset}
        lines.append(_json.dumps(row))
        lines.append(_json.dumps({**row, "stale": True}))
        t += 60.0
    log_path.write_text("\n".join(lines) + "\n")

    monkeypatch.setattr(proxy, "_WEEK_SAMPLES", ua.WeekSamples())
    assert mr.recent_rows(None, 0.0) == []
    assert mr.recent_rows(tmp_path / "missing.jsonl", 0.0) == []
    assert proxy._seed_week_samples(log_path, now) > 0
    for s in range(int(restart), int(now), 60):
        proxy._WEEK_SAMPLES.observe(
            "b144f62f", float(s), 0.01 if s >= restart + 2700 else 0.0, reset
        )
    burn = ua.burn_per_h(proxy._WEEK_SAMPLES.samples("b144f62f"), now, 1800.0)
    state = ua.account_state("proton", 0.01, reset, now, law="remaining", burn=burn)
    assert state["state"] == "on_schedule", state


# --- second Fable meter (7d_oi) -------------------------------------------------
RESET_7D_OI = 1793397600


def _bearer_oi(util_7d_oi, *, reset_7d_oi=RESET_7D_OI, status="allowed", at=1.0):
    state = _bearer(0.2, 0.3, at=at)
    state["unified"].update(util_7d_oi=util_7d_oi, reset_7d_oi=reset_7d_oi, status_7d_oi=status)
    return state


def test_7d_oi_is_recorded_with_the_same_classification_as_the_other_windows(tmp_path):
    state = {"b": _bearer_oi(0.40, at=1.0)}
    rec = _rec(tmp_path, state)
    rec.write(rec.rows(10))  # first reading
    state["b"] = _bearer_oi(0.10, at=2.0)  # same reset epoch, fell
    rec.write(rec.rows(70))
    first, second = _lines(tmp_path / "meter.jsonl")
    assert first["event_7d_oi"] == "first"
    assert first["util_7d_oi"] == 0.40 and first["reset_7d_oi"] == RESET_7D_OI
    assert first["status_7d_oi"] == "allowed"
    assert second["event_7d_oi"] == "recalibration"
    assert second["delta_7d_oi"] == -0.3
    assert second["prev_util_7d_oi"] == 0.40
    assert second["event_7d"] == "steady"  # the other windows are untouched by it


def test_7d_oi_new_reset_is_a_roll_and_rise_is_consumption(tmp_path):
    state = {"b": _bearer_oi(0.40, at=1.0)}
    rec = _rec(tmp_path, state)
    rec.rows(10)
    state["b"] = _bearer_oi(0.05, reset_7d_oi=RESET_7D_OI + 604800, at=2.0)
    assert rec.rows(70)[0]["event_7d_oi"] == "rolled"
    state["b"] = _bearer_oi(0.09, reset_7d_oi=RESET_7D_OI + 604800, at=3.0)
    assert rec.rows(130)[0]["event_7d_oi"] == "consumption"


def test_7d_oi_absent_header_is_an_absent_event_with_null_values(tmp_path):
    state = {"b": _bearer(0.2, 0.3)}  # no 7d_oi header at all
    rows = _rec(tmp_path, state).rows(10)
    assert rows[0]["event_7d_oi"] == "absent"
    assert rows[0]["util_7d_oi"] is None and rows[0]["reset_7d_oi"] is None
    assert rows[0]["event_7d"] == "first"  # the 7d window is still judged


def test_rows_written_before_7d_oi_stay_readable_and_seed_the_next_reading(tmp_path):
    path = tmp_path / "meter.jsonl"
    old = {"epoch": 1, "bearer": "b", "sampled_at": 1.0, "util_7d": 0.3, "reset_7d": RESET_7D}
    path.write_text(json.dumps(old) + "\n")
    rec = mr.MeterRecorder(path, lambda: {"b": _bearer_oi(0.40, at=2.0)})
    rec.seed()
    rec.write(rec.rows(10))
    new = _lines(path)[-1]
    assert new["event_7d_oi"] == "first"  # no 7d_oi in the old row: nothing to compare
    assert new["event_7d"] == "steady"
