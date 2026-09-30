"""MiMo telemetry: measured monthly credits, independent freshness, no credentials."""

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anthropic_throttle_proxy import lanes
from anthropic_throttle_proxy.ui.routes import _build_subscriptions

_probe = runpy.run_path(str(Path(__file__).parents[1] / "scripts/mimo-token-plan-probe.py"))
report, team_seat_lane, team_report = (
    _probe[key] for key in ("report", "team_seat_lane", "team_report")
)
NOW = datetime(2026, 9, 23, 16, tzinfo=UTC)


def plan_detail():
    return {
        "code": 0,
        "data": {
            "planName": "Max",
            "expired": False,
            "currentPeriodEnd": "2026-10-23 23:59:59",
            "apiKey": "must-not-escape",
        },
    }


def plan_usage(used=41_000_000_000, limit=82_000_000_000):
    return {
        "code": 0,
        "data": {
            "monthUsage": {
                "items": [{"name": "month_total_token", "used": used, "limit": limit, "percent": 0}]
            }
        },
    }


def sample(used=41_000_000_000, limit=82_000_000_000):
    return report(plan_detail(), plan_usage(used, limit), NOW)


@pytest.mark.parametrize("used,limit", [(-1, 82), (0, 0), (True, 82), (1, float("inf"))])
def test_bad_counters_fail_closed(used, limit):
    with pytest.raises(ValueError):
        sample(used, limit)


def test_report_allowlists_credits_without_inventing_window_start():
    result = sample()
    assert "must-not-escape" not in json.dumps(result)
    meter = result["lanes"][0]["meters"][0]
    assert meter["usedPercent"] == 50  # derive from counters, not rounded provider percent
    assert meter["remaining"] == 41_000_000_000
    assert meter["windowMins"] is None


@pytest.mark.parametrize(
    "age,pct,status", [(0, 50, "ok"), (1801, 50, "stale"), (0, 100, "exhausted")]
)
def test_mimo_uses_own_freshness_and_renders(tmp_path, monkeypatch, age, pct, status):
    main = tmp_path / "main.json"
    main.write_text(
        json.dumps(
            {"generatedAt": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "intervalSeconds": 900, "lanes": []}
        )
    )
    extra = tmp_path / "mimo.json"
    result = sample(820_000_000 * pct)
    result["generatedAt"] = (NOW - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    extra.write_text(json.dumps(result))
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(main))
    monkeypatch.setenv("THROTTLE_MIMO_REPORT", str(extra))
    lanes._cache = None
    view = lanes.view(NOW.timestamp())
    row = view["lanes"][0]
    assert (row["provider"], row["family"], row["status"]) == ("MiMo", "chinese-frontier", status)
    assert row["binding_pct"] == pct
    assert row["meters"][0]["allowance"] == 82_000_000_000
    rendered = _build_subscriptions({}, view, NOW.timestamp())[0]
    assert rendered["meters"][0]["label"] == "monthly"
    assert rendered["pace"] is None
    lanes._cache = None


@pytest.mark.parametrize("payload", [None, "invalid", {"lanes": []}])
def test_missing_mimo_never_reads_as_healthy(tmp_path, monkeypatch, payload):
    path = tmp_path / "mimo.json"
    if payload is not None:
        path.write_text(json.dumps(payload))
    monkeypatch.setenv("THROTTLE_MIMO_REPORT", str(path))
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(tmp_path / "absent.json"))
    lanes._cache = None
    row = lanes.view(NOW.timestamp())["lanes"][0]
    assert row["status"] == "unknown" and not row["meters"]
    lanes._cache = None


# ── Team seat telemetry (spec 281) ─────────────────────────────────────────


def team_sample(
    *,
    code=0,
    plan_name="Example Team",
    expired=False,
    period_end="2026-10-23 23:59:59",
    seat_status="ASSIGNED",
    total=1000,
    used=250,
    history=250,
    used_percent=99.0,
    history_used_percent=97.0,
    next_reset=None,
    with_seat=True,
):
    """The assignment's generic authenticated SHAPE — synthetic values only.

    `used_percent`/`history_used_percent` deliberately DISAGREE with the
    counters so a test can prove every rendered number is derived from
    creditsTotal/creditsUsed and never read from the display percentages.
    """
    data = {
        "projectId": "project-example-must-not-leak",
        "planCode": "example",
        "planName": plan_name,
        "currentPeriodEnd": period_end,
        "settlementType": "example",
        "autoRenew": True,
        "expired": expired,
    }
    if with_seat:
        data["seat"] = {
            "seatId": "seat-identifier-must-not-leak",
            "seatStatus": seat_status,
            "creditsTotal": total,
            "creditsUsed": used,
            "historyCreditsUsed": history,
            "usedPercent": used_percent,
            "historyUsedPercent": history_used_percent,
            "nextResetTime": next_reset,
        }
    return {"code": code, "data": data}


def test_team_seat_derives_counters_and_leaks_no_ids():
    row = team_seat_lane(team_sample(), NOW)
    assert (row["id"], row["kind"], row["status"]) == ("mimo:team-owner", "mimo", "ok")
    meter = row["meters"][0]
    # 250/1000 — NOT the 99.0/97.0 display percentages in the payload.
    assert meter["usedPercent"] == 25
    assert (meter["allowance"], meter["current"], meter["remaining"]) == (1000, 250, 750)
    assert meter["limitId"] == "seat"
    assert meter["windowMins"] is None
    dump = json.dumps(row)
    assert "project-example-must-not-leak" not in dump
    assert "seat-identifier-must-not-leak" not in dump


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"code": 1}, "unavailable"),
        ({"with_seat": False}, "invalid"),
        ({"expired": "yes"}, "invalid"),
        ({"plan_name": None}, "invalid"),
        ({"seat_status": None}, "invalid"),
        ({"seat_status": "PENDING"}, "unassigned"),
        ({"seat_status": "UNASSIGNED"}, "unassigned"),
        ({"total": True}, "invalid"),
        ({"used": float("nan")}, "invalid"),
        ({"used": -1}, "invalid"),
        ({"history": -5}, "invalid"),
        ({"total": 0}, "ambiguous"),
        ({"used": 250, "history": 100}, "ambiguous"),
        ({"period_end": "not-a-date"}, "unparseable"),
        ({"next_reset": 42}, "ambiguous"),
        ({"next_reset": "garbage"}, "unparseable"),
    ],
)
def test_team_failures_fail_closed_without_meters(kwargs, reason):
    row = team_seat_lane(team_sample(**kwargs), NOW)
    assert row["id"] == "mimo:team-owner" and row["kind"] == "mimo"
    assert row["status"] == "unknown"  # fail closed: never a healthy reading
    assert "meters" not in row  # and never fabricated counters
    assert reason in row["reason"]


def test_team_unassigned_seat_is_not_usable_capacity():
    row = team_seat_lane(team_sample(seat_status="PENDING", total=1000, used=0), NOW)
    assert row["status"] == "unknown" and "meters" not in row
    assert "not usable capacity" in row["reason"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expired": True},
        {"used": 1000, "history": 1000},
        {"period_end": "2026-09-01 00:00:00"},
    ],
)
def test_team_expired_or_exhausted_keeps_only_measured_counters(kwargs):
    row = team_seat_lane(team_sample(**kwargs), NOW)
    assert row["status"] == "exhausted"  # closed, but honest about the numbers
    assert row["meters"][0]["current"] == kwargs.get("used", 250)


def test_team_failure_writes_a_fresh_fail_closed_row_instead_of_raising():
    # The retention falsifier: a failing team read must still rewrite the team
    # row as unusable — raising would skip the write and silently keep the
    # previous healthy meter on screen.
    result = team_report(plan_detail(), plan_usage(), None, NOW)
    team_row = result["lanes"][1]
    assert team_row["status"] == "unknown" and "meters" not in team_row
    assert result["lanes"][0]["status"] == "ok"  # plan reading unaffected


def test_team_row_never_replaces_or_adds_to_the_plan_allowance():
    result = team_report(plan_detail(), plan_usage(), team_sample(), NOW)
    ids = [lane["id"] for lane in result["lanes"]]
    assert ids == ["mimo:plan", "mimo:team-owner"]
    plan_meter, team_meter = (lane["meters"][0] for lane in result["lanes"])
    assert plan_meter["allowance"] == 82_000_000_000
    assert team_meter["allowance"] == 1000
    assert plan_meter["current"] == 41_000_000_000 and team_meter["current"] == 250


def test_legacy_report_shape_is_unchanged_without_team():
    result = sample()
    assert result["schema"] == 1 and result["intervalSeconds"] == 900
    assert [lane["id"] for lane in result["lanes"]] == ["mimo:plan"]


def _write_mimo_report(tmp_path, monkeypatch, rows, *, age_s=0):
    main = tmp_path / "main.json"
    main.write_text(
        json.dumps(
            {"generatedAt": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "intervalSeconds": 900, "lanes": []}
        )
    )
    extra = tmp_path / "mimo.json"
    result = {"schema": 1, "intervalSeconds": 900, "lanes": rows}
    result["generatedAt"] = (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    extra.write_text(json.dumps(result))
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(main))
    monkeypatch.setenv("THROTTLE_MIMO_REPORT", str(extra))
    lanes._cache = None
    return lanes.view(NOW.timestamp())


def test_view_renders_team_row_with_distinct_identity(tmp_path, monkeypatch):
    combined = team_report(plan_detail(), plan_usage(), team_sample(), NOW)
    view = _write_mimo_report(tmp_path, monkeypatch, combined["lanes"])
    rows = {row["id"]: row for row in view["lanes"]}
    assert set(rows) == {"mimo:plan", "mimo:team-owner"}
    assert rows["mimo:plan"]["identity"] == "MiMo"
    assert rows["mimo:team-owner"]["identity"] == "MiMo Team"
    assert rows["mimo:team-owner"]["status"] == "ok"


def test_view_absent_team_row_preserves_individual_only(tmp_path, monkeypatch):
    view = _write_mimo_report(tmp_path, monkeypatch, sample()["lanes"])
    assert [row["id"] for row in view["lanes"]] == ["mimo:plan"]


def test_view_ambiguous_team_rows_fail_closed(tmp_path, monkeypatch):
    combined = team_report(plan_detail(), plan_usage(), team_sample(), NOW)
    team_row = combined["lanes"][1]
    view = _write_mimo_report(tmp_path, monkeypatch, [combined["lanes"][0], team_row, team_row])
    rows = [row for row in view["lanes"] if row["id"] == "mimo:team-owner"]
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    assert "ambiguous" in rows[0]["reason"]


def test_view_team_failure_row_replaces_the_healthy_meter(tmp_path, monkeypatch):
    fresh = team_report(plan_detail(), plan_usage(), team_sample(), NOW)["lanes"]
    failed = team_report(plan_detail(), plan_usage(), None, NOW)["lanes"]
    view = _write_mimo_report(tmp_path, monkeypatch, fresh)
    assert view["lanes"][1]["status"] == "ok"
    view = _write_mimo_report(tmp_path, monkeypatch, failed)
    rows = [row for row in view["lanes"] if row["id"] == "mimo:team-owner"]
    assert rows[0]["status"] == "unknown" and not rows[0]["meters"]
