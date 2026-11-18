"""Spec 377 pace replay: the fluid model drives the real usage_allocator decisions."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from anthropic_throttle_proxy import usage_allocator as ua

_SPEC = importlib.util.spec_from_file_location(
    "pace_replay", Path(__file__).resolve().parents[1] / "scripts" / "pace_replay.py"
)
assert _SPEC and _SPEC.loader
pr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pr)

T0 = 1_000_000.0
W = ua.WEEK_S
BURST = 1 / 28


def _accounts(rate_x: float) -> list[dict]:
    """Two accounts starting a fresh week, each burning rate_x times the line."""
    return [{"label": lab, "util": 0.0, "reset": T0 + W, "rate": rate_x / W} for lab in ("a", "b")]


def _run(mode: str, demand: float, burst: float = BURST, priority: float = 0.0, k: float = 1.0):
    return pr.simulate(
        mode,
        burst,
        k,
        demand,
        _accounts(1.0),
        pr.Window(priority, T0, W + 120),
    )


def test_constant_1_2x_off_ends_above_1():
    r = _run("off", 1.2)
    assert all(v > 1.0 for v in r["end_pace"].values()), r["end_pace"]


def test_constant_1_2x_enforce_is_held_to_allowance_not_1_00():
    # Enforce caps each account at elapsed + burst, so a window closes at 1 + burst.
    # The brief's literal "<= 1.0 at 1.2x" cannot hold for burst > 0; this asserts the bound.
    r = _run("enforce", 1.2)
    for v in r["end_pace"].values():
        assert 1.0 < v <= 1.0 + BURST + 0.005, r["end_pace"]
    off = _run("off", 1.2)
    assert max(r["end_pace"].values()) < min(off["end_pace"].values())


def test_demand_cap_0_8_enforce_ends_at_or_below_1():
    r = _run("enforce", 0.8)
    assert r["max_end_pace"] <= 1.0
    assert r["deferred_request_events"] == 0.0


def test_observe_matches_off_and_never_defers():
    off, obs = _run("off", 1.2), _run("observe", 1.2)
    assert obs["end_pace"] == off["end_pace"]
    assert obs["deferred_request_events"] == 0.0


def test_priority_is_never_deferred_by_enforce():
    r = _run("enforce", 1.2, priority=1.0)
    assert r["deferred_request_events"] == 0.0
    # Priority still lands on ahead accounts once every account is ahead.
    assert 0.0 < r["priority_while_all_ahead_pct"] < 100.0


def test_k_only_changes_retry_timing_not_end_pace():
    # Under the decaying line (381) a longer retry wait returns dropped demand later,
    # so k moves the window close by a few 1e-4, not zero. Bound it, do not pin it.
    k1, k3 = _run("enforce", 1.2, k=1.0), _run("enforce", 1.2, k=3.0)
    for lab in k1["end_pace"]:
        assert abs(k1["end_pace"][lab] - k3["end_pace"][lab]) < 1e-3
    # Each wait is Retry-After (capped at 300 s) times k, so the max is bounded by 300·k.
    assert 0 < k3["max_deferral_s"] <= 300.0 * 3.0
    assert k1["max_deferral_s"] <= 300.0


def test_recommend_least_deferral_then_default_burst():
    runs = [
        {
            "mode": "enforce",
            "burst_label": "1/14",
            "k": 1,
            "demand": 0.8,
            "max_end_pace": 0.9,
            "deferred_request_events": 0.0,
            "max_deferral_s": 0.0,
        },
        {
            "mode": "enforce",
            "burst_label": "1/28",
            "k": 1,
            "demand": 0.8,
            "max_end_pace": 0.9,
            "deferred_request_events": 0.0,
            "max_deferral_s": 0.0,
        },
        {
            "mode": "enforce",
            "burst_label": "1/56",
            "k": 1,
            "demand": 1.0,
            "max_end_pace": 1.2,
            "deferred_request_events": 5.0,
            "max_deferral_s": 300.0,
        },
    ]
    rec = pr.recommend(runs)
    assert rec["meets_1x"] is True
    assert rec["burst_label"] == "1/28"


def test_read_meter_uses_window_slope_and_rejects_missing(tmp_path: Path):
    log = tmp_path / "m.jsonl"
    rows = [
        {"epoch": int(T0), "bearer": "x", "util_7d": 0.0, "reset_7d": int(T0 + W)},
        {"epoch": int(T0 + 3600), "bearer": "x", "util_7d": 0.1, "reset_7d": int(T0 + W)},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (acc,) = pr.read_meter(log, T0 + 3600, {"g": "x"})
    assert acc["util"] == pytest.approx(0.1)
    assert acc["rate"] == pytest.approx(0.1 / 3600)
    with pytest.raises(SystemExit):
        pr.read_meter(log, T0 + 3600, {"g": "nope"})
