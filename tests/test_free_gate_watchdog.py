"""Free-gate per-bearer watchdog (19/10/2026 go-gate incident).

Ground truth replayed here: the live journal on ``:8770`` 18:46-19:11 shows the
shared bearer ``e574f38a`` shrunk by budget pushback ``12 -> 8 -> 5 -> 3 -> 2 -> 1``
and then resting at the floor for 15.2 min while ten ``queue-wait-timeout`` 503s
landed and pi panes died in retry loops.

Every rule test is paired with its near-miss: a rule that cannot FAIL on the
plausible-but-healthy case is not a detector, it is a decoration. The
discriminating pairs are marked FALSIFIER.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from anthropic_throttle_proxy import config, metrics
from anthropic_throttle_proxy import free_gate_watchdog as fgw

T0 = 1_760_900_000.0  # arbitrary fixed epoch, 2025-10-18


def _line(ts_offset: float, kind: str, **fields: object) -> str:
    """Build a ``journalctl -o short-iso`` line for the proxy log envelope."""
    when = time.localtime(T0 + ts_offset)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", when) + time.strftime("%z", when)
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{stamp} desktop throttler-gateway[4242]: [anthropic-throttle] {kind} {body}"


# ---------------------------------------------------------------------------
# Prometheus parsing
# ---------------------------------------------------------------------------
def test_parse_prometheus_reads_labels_and_skips_noise() -> None:
    text = "\n".join(
        [
            "# HELP anthropic_inflight Current in-flight requests.",
            "# TYPE anthropic_inflight gauge",
            "anthropic_inflight 3.0",
            'anthropic_aimd_max_concurrent{bearer="e574f38a"} 1.0',
            'anthropic_aimd_max_concurrent{bearer="ab12cd34"} 12.0',
            'anthropic_requests_total{method="POST",status="429",model="x"} 5.0',
            'anthropic_aimd_grows_created{bearer="e574f38a"} 1.79e+09',
            "some_metric NaN",
            "",
        ]
    )
    scrape = fgw.parse_prometheus(text, ts=T0, source="test")
    names = {s.name for s in scrape.samples}
    assert "anthropic_inflight" in names
    assert "anthropic_aimd_grows_created" not in names, "_created is bookkeeping, not signal"
    assert scrape.value("anthropic_inflight") == 3.0
    assert scrape.bearers(fgw.M_AIMD_MAX) == {"e574f38a": 1.0, "ab12cd34": 12.0}
    # A NaN ceiling would satisfy `<= floor` in the wrong direction.
    assert all(s.value == s.value for s in scrape.samples)


def test_scrape_value_sums_over_unnamed_labels_and_is_none_when_absent() -> None:
    text = "\n".join(
        [
            'anthropic_requests_total{method="POST",status="429",model="a"} 2.0',
            'anthropic_requests_total{method="POST",status="429",model="b"} 3.0',
            'anthropic_requests_total{method="POST",status="200",model="b"} 9.0',
        ]
    )
    scrape = fgw.parse_prometheus(text, ts=T0)
    assert scrape.value(fgw.M_REQUESTS, status="429") == 5.0
    assert scrape.value(fgw.M_REQUESTS, status="200") == 9.0
    # Absent is NOT zero: the rules branch on this difference.
    assert scrape.value(fgw.M_REQUESTS, status="503") is None
    assert scrape.value(fgw.M_QUEUE_WAIT_TIMEOUTS) is None


def test_parse_journal_line_ignores_unstructured_lines() -> None:
    good = _line(0, "aimd-shrink", bid="e574f38a", status=429, max_concurrent=5)
    event = fgw.parse_journal_line(good)
    assert event is not None
    assert event.kind == "aimd-shrink"
    assert event.fields["bid"] == "e574f38a"
    assert event.fields["max_concurrent"] == "5"
    for junk in ("", "   ", "Oct 19 18:50:01 desktop systemd[1]: Started foo."):
        assert fgw.parse_journal_line(junk) is None


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------
def test_reconstruction_rebuilds_ceiling_timeline_and_counters() -> None:
    lines = [
        _line(0, "bearer-new", bid="e574f38a", max_concurrent=12, hard_max=1024),
        _line(60, "aimd-shrink", bid="e574f38a", status=429, max_concurrent=8),
        _line(120, "aimd-shrink", bid="e574f38a", status=429, max_concurrent=5),
        _line(
            180,
            "queue-wait-timeout",
            bid="e574f38a",
            reason="pre-queue-depth",
            est_wait_s=347.07,
            max_wait_s=179.99,
        ),
        _line(240, "done", bid="e574f38a", status=200, inflight=0, queued=0),
        _line(300, "done", bid="e574f38a", status=429, inflight=0, queued=0),
    ]
    scrapes, recon = fgw.scrapes_from_journal(lines, floor=1.0, source="unit")
    ceilings = [
        s.bearers(fgw.M_AIMD_MAX)["e574f38a"]
        for s in scrapes
        if "e574f38a" in s.bearers(fgw.M_AIMD_MAX)
    ]
    # The gauge rides on every scrape; the *transitions* are the timeline.
    assert [c for i, c in enumerate(ceilings) if i == 0 or c != ceilings[i - 1]] == [12.0, 8.0, 5.0]
    last = scrapes[-1]
    assert last.value(fgw.M_AIMD_SHRINKS, bearer="e574f38a", status="429") == 2.0
    assert last.value(fgw.M_QUEUE_WAIT_TIMEOUTS, bearer="e574f38a") == 1.0
    assert last.value(fgw.M_REQUESTS, status="200") == 1.0
    assert last.value(fgw.M_REQUESTS, status="429") == 1.0
    # Replay-only extras that have no production metric.
    assert recon.extra["queue-depth-reject"] == 0.0
    # Scrapes are chronological — the rules rely on it.
    assert all(a.ts <= b.ts for a, b in zip(scrapes, scrapes[1:], strict=False))


def test_reconstruction_counts_pre_queue_refusals_separately() -> None:
    lines = [
        _line(0, "bearer-new", bid="b1", max_concurrent=1),
        _line(10, "queue-depth-reject", bid="b1", slots=1, est_wait_s=210.0),
        _line(20, "queue-depth-reject", bid="b1", slots=1, est_wait_s=220.0),
        _line(30, "queue-first-waiter", bid="b1", slots=1),
        _line(40, "rate-pushback-retry", bid="b1", status=429, retry="1/1"),
    ]
    _, recon = fgw.scrapes_from_journal(lines, floor=1.0)
    assert recon.extra["queue-depth-reject"] == 2.0
    assert recon.extra["rate-pushback-retry"] == 1.0


# ---------------------------------------------------------------------------
# R1 — pinned at/below the floor
# ---------------------------------------------------------------------------
def _pin_stream(*, ceiling: float, floor: float, minutes: float, step_s: float = 30.0):
    """Scrapes where the ceiling rests at ``ceiling`` for ``minutes``."""
    scrapes = []
    steps = int(minutes * 60 / step_s)
    for i in range(steps + 1):
        ts = T0 + i * step_s
        scrapes.append(
            fgw.Scrape(
                ts=ts,
                source="unit",
                samples=[
                    fgw.Sample(fgw.M_AIMD_MAX, (("bearer", "b1"),), ceiling),
                    fgw.Sample(fgw.M_AIMD_FLOOR, (("bearer", "b1"),), floor),
                ],
            )
        )
    return scrapes


def test_r1_pinned_fires_after_the_dwell_and_reports_the_full_run() -> None:
    cfg = fgw.WatchdogConfig(pin_dwell_s=600.0)
    alerts, pending = fgw.evaluate(_pin_stream(ceiling=1, floor=1, minutes=15.0), cfg)
    pinned = [a for a in alerts if a.rule == "pinned"]
    assert len(pinned) == 1
    alert = pinned[0]
    assert alert.severity == "critical"
    assert alert.bearer == "b1"
    # Fires at the dwell crossing, but the detail carries the real run bounds.
    assert alert.ts == pytest.approx(T0 + 600.0, abs=30.0)
    assert "15.0 min" in alert.detail
    assert "->" in alert.detail
    assert not pending


def test_r1_falsifier_short_pin_is_pending_not_an_alert() -> None:
    """A lane resting at its floor for 4 min is idle, not pinned."""
    cfg = fgw.WatchdogConfig(pin_dwell_s=600.0)
    alerts, pending = fgw.evaluate(_pin_stream(ceiling=1, floor=1, minutes=4.0), cfg)
    assert [a for a in alerts if a.rule == "pinned"] == []
    assert len(pending) == 1
    assert pending[0].rule == "pinned"
    assert pending[0].seconds == pytest.approx(240.0, abs=30.0)


def test_r1_falsifier_healthy_ceiling_never_alerts_however_long() -> None:
    """The whole point: a ceiling ABOVE the floor is healthy at any duration."""
    cfg = fgw.WatchdogConfig(pin_dwell_s=600.0)
    alerts, pending = fgw.evaluate(_pin_stream(ceiling=12, floor=1, minutes=90.0), cfg)
    assert alerts == []
    assert pending == []


def test_r1_honours_a_hot_tuned_floor_from_the_scraped_series() -> None:
    """Floor 2 (the post-incident THROTTLE_AIMD_MIN on :8770) still catches a 1."""
    cfg = fgw.WatchdogConfig(pin_dwell_s=600.0)
    alerts, _ = fgw.evaluate(_pin_stream(ceiling=1, floor=2, minutes=15.0), cfg)
    assert [a.rule for a in alerts] == ["pinned"]
    assert "floor 2" in alerts[0].detail


def test_r1_run_ends_when_the_ceiling_grows_back() -> None:
    scrapes = _pin_stream(ceiling=1, floor=1, minutes=12.0)
    recovered = fgw.Scrape(
        ts=scrapes[-1].ts + 30,
        source="unit",
        samples=[
            fgw.Sample(fgw.M_AIMD_MAX, (("bearer", "b1"),), 2.0),
            fgw.Sample(fgw.M_AIMD_FLOOR, (("bearer", "b1"),), 1.0),
        ],
    )
    alerts, _ = fgw.evaluate([*scrapes, recovered], fgw.WatchdogConfig(pin_dwell_s=600.0))
    assert len(alerts) == 1
    assert "12.5 min" in alerts[0].detail


# ---------------------------------------------------------------------------
# R2 — budget-pushback rate
# ---------------------------------------------------------------------------
def _shrink_stream(offsets: list[float], *, bearer: str = "b1") -> list[fgw.Scrape]:
    scrapes = []
    count = 0.0
    for offset in offsets:
        count += 1.0
        scrapes.append(
            fgw.Scrape(
                ts=T0 + offset,
                source="unit",
                samples=[
                    fgw.Sample(fgw.M_AIMD_SHRINKS, (("bearer", bearer), ("status", "429")), count),
                    fgw.Sample(fgw.M_AIMD_MAX, (("bearer", bearer),), 1.0),
                    fgw.Sample(fgw.M_AIMD_FLOOR, (("bearer", bearer),), 1.0),
                ],
            )
        )
    return scrapes


def test_r2_pushback_spike_alerts_and_respects_its_cooldown() -> None:
    cfg = fgw.WatchdogConfig(pin_dwell_s=10_000.0, pushback_count=3, pushback_window_s=300.0)
    # samples at 0/60/120/180: the first is the baseline, so the increments are
    # 3 (at 60, 120, 180) and the threshold is crossed on the 3rd.
    alerts, _ = fgw.evaluate(_shrink_stream([0, 60, 120, 180]), cfg)
    pushback = [a for a in alerts if a.rule == "pushback"]
    assert len(pushback) == 1
    assert pushback[0].severity == "warning"
    assert pushback[0].ts == pytest.approx(T0 + 180, abs=1.0)


def test_r2_alerts_again_after_the_cooldown() -> None:
    cfg = fgw.WatchdogConfig(pin_dwell_s=10_000.0, pushback_count=3, pushback_window_s=300.0)
    # 4 samples per burst -> 3 increments each; bursts at 240 and 880 (cooldown
    # 300 s is over) must both alert.
    alerts, _ = fgw.evaluate(_shrink_stream([60, 120, 180, 240, 700, 760, 820, 880]), cfg)
    assert len([a for a in alerts if a.rule == "pushback"]) == 2


def test_r2_falsifier_concurrency_429s_do_not_shrink_so_must_not_alert() -> None:
    """A burst of concurrency-class 429s must NOT page anyone.

    ``anthropic_aimd_shrinks_total`` counts budget-class pushback only; a
    headerless 429 with the windows allowed is classified ``concurrency-429``
    and deliberately does not shrink (17/07/2026 regression). Modelling that
    burst here as "requests 429 up, shrinks flat" is exactly what the live
    metric would look like — and the rule must stay silent.
    """
    scrapes = []
    for i, offset in enumerate([0, 30, 60, 90, 120, 150]):
        scrapes.append(
            fgw.Scrape(
                ts=T0 + offset,
                source="unit",
                samples=[
                    fgw.Sample(
                        fgw.M_REQUESTS,
                        (("method", "POST"), ("status", "429"), ("model", "m")),
                        float(i + 1),
                    ),
                    fgw.Sample(fgw.M_AIMD_SHRINKS, (("bearer", "b1"), ("status", "429")), 0.0),
                    fgw.Sample(fgw.M_AIMD_MAX, (("bearer", "b1"),), 12.0),
                    fgw.Sample(fgw.M_AIMD_FLOOR, (("bearer", "b1"),), 1.0),
                ],
            )
        )
    alerts, _ = fgw.evaluate(scrapes, fgw.WatchdogConfig(pin_dwell_s=10_000.0))
    assert alerts == []


def test_r2_falsifier_two_shrinks_in_window_is_below_threshold() -> None:
    alerts, _ = fgw.evaluate(
        _shrink_stream([0, 60]), fgw.WatchdogConfig(pin_dwell_s=10_000.0, pushback_count=3)
    )
    assert [a for a in alerts if a.rule == "pushback"] == []


def test_counter_reset_after_a_gate_restart_is_not_a_spike() -> None:
    """The 18:48 restart zeroes every counter; a decrease is not an event."""

    def scrape(offset: float, value: float) -> fgw.Scrape:
        return fgw.Scrape(
            ts=T0 + offset,
            source="unit",
            samples=[fgw.Sample(fgw.M_AIMD_SHRINKS, (("bearer", "b1"), ("status", "429")), value)],
        )

    scrapes = [scrape(0, 3.0), scrape(30, 0.0), scrape(60, 1.0)]
    alerts, _ = fgw.evaluate(scrapes, fgw.WatchdogConfig(pin_dwell_s=10_000.0, pushback_count=2))
    # The post-restart rise 0 -> 1 is ONE event, below the 2-event threshold.
    assert alerts == []


# ---------------------------------------------------------------------------
# R3 — queue-wait-timeout rate
# ---------------------------------------------------------------------------
def _timeout_stream(count: int, *, span_s: float, bearer: str = "b1") -> list[fgw.Scrape]:
    scrapes = []
    for i in range(1, count + 1):
        scrapes.append(
            fgw.Scrape(
                ts=T0 + (i - 1) * (span_s / max(1, count - 1)),
                source="unit",
                samples=[
                    fgw.Sample(fgw.M_QUEUE_WAIT_TIMEOUTS, (("bearer", bearer),), float(i)),
                    fgw.Sample(fgw.M_AIMD_MAX, (("bearer", bearer),), 1.0),
                    fgw.Sample(fgw.M_AIMD_FLOOR, (("bearer", bearer),), 1.0),
                ],
            )
        )
    return scrapes


def test_r3_queue_timeout_burst_is_critical() -> None:
    cfg = fgw.WatchdogConfig(pin_dwell_s=10_000.0, timeout_rate_per_min=2.0, timeout_window_s=60.0)
    alerts, _ = fgw.evaluate(_timeout_stream(6, span_s=30.0), cfg)
    qto = [a for a in alerts if a.rule == "qtimeout"]
    assert qto and qto[0].severity == "critical"
    assert "queue-wait-timeout 503" in qto[0].detail


def test_r3_falsifier_an_isolated_timeout_is_not_a_storm() -> None:
    """One 503 in a 60 s window is noise; two per minute is the threshold."""
    cfg = fgw.WatchdogConfig(pin_dwell_s=10_000.0, timeout_rate_per_min=2.0, timeout_window_s=60.0)
    alerts, _ = fgw.evaluate(_timeout_stream(1, span_s=30.0), cfg)
    assert alerts == []


# ---------------------------------------------------------------------------
# Metrics surface
# ---------------------------------------------------------------------------
def test_aimd_floor_metric_is_exposed_for_every_bearer() -> None:
    from prometheus_client import generate_latest

    body = generate_latest(metrics.REGISTRY).decode()
    assert f"# HELP {fgw.M_AIMD_FLOOR}" in body
    assert f"# TYPE {fgw.M_AIMD_FLOOR} gauge" in body


def test_aimd_floor_metric_follows_a_hot_tuned_throttle_aimd_min(monkeypatch) -> None:
    """Read at scrape time, not import time, so a knob retune is visible."""
    from prometheus_client import generate_latest

    monkeypatch.setattr(config, "AIMD_MIN", 4)
    body = generate_latest(metrics.REGISTRY).decode()
    values = [
        line.rsplit(" ", 1)[1]
        for line in body.splitlines()
        if line.startswith(f"{fgw.M_AIMD_FLOOR}{{")
    ]
    assert values, "floor series must exist even on a cold gate"
    assert all(float(v) == 4.0 for v in values)


def test_aimd_floor_metric_lists_a_registered_bearer(monkeypatch) -> None:
    from prometheus_client import generate_latest

    monkeypatch.setitem(config.bearer_limiters, "deadbeef", object())
    body = generate_latest(metrics.REGISTRY).decode()
    assert f'{fgw.M_AIMD_FLOOR}{{bearer="deadbeef"}}' in body


# ---------------------------------------------------------------------------
# End-to-end replay of the incident
# ---------------------------------------------------------------------------
def _incident_lines() -> list[str]:
    """Minimal reproduction of the 19/10 go-gate shape (12 -> 1, then a storm)."""
    lines = [_line(0, "bearer-new", bid="e574f38a", max_concurrent=12)]
    lines += [
        _line(offset, "aimd-shrink", bid="e574f38a", status=429, max_concurrent=ceiling)
        for offset, ceiling in [(60, 8), (120, 5), (180, 3), (240, 2), (300, 1)]
    ]
    # Ten pre-queue-depth refusals while the ceiling rests at the floor.
    lines += [
        _line(
            360 + 10 * i,
            "queue-wait-timeout",
            bid="e574f38a",
            reason="pre-queue-depth",
            max_concurrent=1,
            est_wait_s=210.0 + 15 * i,
            max_wait_s=179.99,
            retry_after_s=300,
        )
        for i in range(10)
    ]
    lines.append(_line(1100, "aimd-grow", bid="e574f38a", max_concurrent=2))
    return lines


def test_replay_of_the_incident_reproduces_all_three_signals() -> None:
    alerts, pending, recon = fgw.replay_journal(_incident_lines(), floor=1.0, source="unit")
    rules = {a.rule for a in alerts}
    assert rules == {"pinned", "pushback", "qtimeout"}, rules
    assert recon.extra["queue-depth-reject"] == 0.0
    # The early warning precedes the first client-visible 503.
    first_pushback = min(a.ts for a in alerts if a.rule == "pushback")
    first_qtimeout = min(a.ts for a in alerts if a.rule == "qtimeout")
    assert first_pushback < first_qtimeout, "pushback must lead the 503 storm"
    pinned = next(a for a in alerts if a.rule == "pinned")
    assert pinned.severity == "critical"


def test_replay_falsifier_a_healthy_capture_produces_no_alerts() -> None:
    """A clean run of 200s with a steady ceiling is silent — no alert fatigue."""
    lines = [_line(0, "bearer-new", bid="e574f38a", max_concurrent=12)]
    for i in range(1, 40):
        lines.append(_line(10 * i, "done", bid="e574f38a", status=200, inflight=0, queued=0))
    alerts, pending, _ = fgw.replay_journal(lines, floor=1.0)
    assert alerts == []
    assert pending == []


def test_cli_replay_emits_json_with_a_timeline(tmp_path: Path, capsys) -> None:
    capture = tmp_path / "capture.log"
    capture.write_text("\n".join(_incident_lines()) + "\n", encoding="utf-8")
    rc = fgw.main(["--replay", str(capture), "--floor", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1, "a critical alert must exit non-zero for systemd OnFailure"
    assert {a["rule"] for a in payload["alerts"]} == {"pinned", "pushback", "qtimeout"}
    # The reconstructed ceiling timeline is the replay's evidence surface.
    assert [row["ceiling"] for row in payload["timeline"]] == [12, 8, 5, 3, 2, 1, 2]
    assert payload["window"]["start"] and payload["window"]["end"]


def test_cli_replay_exit_zero_on_a_clean_capture(tmp_path: Path, capsys) -> None:
    capture = tmp_path / "clean.log"
    lines = [_line(0, "bearer-new", bid="b1", max_concurrent=12)]
    for i in range(1, 20):
        lines.append(_line(10 * i, "done", bid="b1", status=200, inflight=0, queued=0))
    capture.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rc = fgw.main(["--replay", str(capture), "--floor", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["alerts"] == []
