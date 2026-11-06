"""Counterexamples from the 06/11/2026 pacing restart adversarial review."""

import json

import pytest

from anthropic_throttle_proxy import meter_recorder as mr
from anthropic_throttle_proxy import usage_allocator as ua


@pytest.mark.parametrize("restore", [False, True])
def test_recalibration_starts_a_new_burn_segment(restore):
    now = 2_000_000.0
    reset = now + 160 * 3600
    rows = [
        {"bearer": "a", "sampled_at": now - 3600, "util_7d": 0.60, "reset_7d": reset},
        {
            "bearer": "a",
            "sampled_at": now - 600,
            "util_7d": 0.05,
            "reset_7d": reset,
            "event_7d": "recalibration",
        },
        {"bearer": "a", "sampled_at": now, "util_7d": 0.06, "reset_7d": reset},
    ]
    samples = ua.WeekSamples()
    if restore:
        samples.seed(rows, now)
    else:
        for row in rows:
            samples.observe("a", row["sampled_at"], row["util_7d"], reset)
    burn = ua.burn_per_h(samples.samples("a"), now, 1800)
    assert burn == pytest.approx(0.06)
    assert ua.account_state("a", 0.06, reset, now, law="remaining", burn=burn)["state"] == "ahead"


def test_recalibration_is_not_lost_to_downsampling():
    samples = ua.WeekSamples()
    samples.observe("a", 1000, 0.60, 10000)
    samples.observe("a", 1010, 0.05, 10000)
    samples.observe("a", 1610, 0.06, 10000)
    assert ua.burn_per_h(samples.samples("a"), 1610, 1800) == pytest.approx(0.06)


def test_recent_rows_restores_rotated_plateau(tmp_path):
    path = tmp_path / "meter.jsonl"
    now = 2_000_000.0
    reset = now + 160 * 3600
    row = {"bearer": "a", "util_7d": 0.0, "reset_7d": reset}
    rotated = [
        {**row, "sampled_at": now - 7 * 3600},
        {**row, "sampled_at": now - 3600},
    ]
    current = [
        {**row, "sampled_at": now - 600},
        {**row, "sampled_at": now, "util_7d": 0.01},
    ]
    path.with_name(path.name + ".1").write_text("\n".join(map(json.dumps, rotated)) + "\n")
    path.write_text("\n".join(map(json.dumps, current)) + "\n")
    samples = ua.WeekSamples()
    samples.seed(mr.recent_rows(path, now - ua.SAMPLE_KEEP_S), now)
    assert ua.burn_per_h(samples.samples("a"), now, 1800) == pytest.approx(0.01 / 7)


def test_recent_rows_combines_files_with_one_byte_bound(tmp_path, monkeypatch):
    path = tmp_path / "meter.jsonl"
    old = {"sampled_at": 100, "bearer": "old"}
    newest = {"sampled_at": 200, "bearer": "new"}
    path.with_name(path.name + ".1").write_text(json.dumps(old) + "\n")
    current = json.dumps(newest) + "\n"
    path.write_text(current)
    monkeypatch.setattr(mr, "_RESTORE_TAIL_BYTES", len(current.encode()))
    assert mr.recent_rows(path, 0) == [newest]


def test_recent_rows_skips_torn_and_non_object_rows(tmp_path):
    path = tmp_path / "meter.jsonl"
    row = {"sampled_at": 100, "bearer": "a"}
    path.write_text("torn\n[]\nnull\n" + json.dumps(row) + '\n{"crash":')
    assert mr.recent_rows(path, 0) == [row]
