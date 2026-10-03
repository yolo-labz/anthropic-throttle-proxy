"""MiMo telemetry: measured monthly credits, independent freshness, no credentials."""

import json
import runpy
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from anthropic_throttle_proxy import lanes
from anthropic_throttle_proxy.ui.routes import _build_subscriptions

_probe = runpy.run_path(str(Path(__file__).parents[1] / "scripts/mimo-token-plan-probe.py"))
report, team_seat_b_lane, team_seat_lane, team_report = (
    _probe[key] for key in ("report", "team_seat_b_lane", "team_seat_lane", "team_report")
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
        ({"total": 0}, "invalid"),
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


def test_team_history_below_used_is_the_real_assigned_seat_shape():
    # Live protocol check 30/09: the only real ASSIGNED seat reported
    # historyCreditsUsed < creditsUsed. History is informational; it must
    # never gate the meters.
    row = team_seat_lane(team_sample(used=250, history=3), NOW)
    assert row["status"] == "ok"
    assert row["meters"][0]["current"] == 250


@pytest.mark.parametrize("history", [None, -5, "unavailable", float("nan")])
def test_team_historical_display_field_cannot_veto_current_counters(history):
    payload = team_sample(history=history)
    assert team_seat_lane(payload, NOW)["status"] == "ok"
    del payload["data"]["seat"]["historyCreditsUsed"]
    assert team_seat_lane(payload, NOW)["meters"][0]["current"] == 250


def test_team_period_end_uses_t_separator():
    # Live protocol check 30/09: the console serialises team period ends with
    # a literal `T`. The space spelling stays accepted for compatibility.
    row = team_seat_lane(team_sample(period_end="2026-10-30T23:59:59"), NOW)
    assert row["status"] == "ok"
    assert (
        row["meters"][0]["resetsAt"] == datetime(2026, 10, 30, 23, 59, 59, tzinfo=UTC).timestamp()
    )


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


def team_sample_b(**seat_overrides):
    """The B seat's reading: explicit synthetic counters only, never real quota."""
    return team_sample(**seat_overrides)


def test_b_seat_row_is_independent_of_owner_and_plan():
    result = team_report(
        plan_detail(), plan_usage(), team_sample(), NOW, team_sample_b(total=2000, used=500)
    )
    ids = [lane["id"] for lane in result["lanes"]]
    assert ids == ["mimo:plan", "mimo:team-owner", "mimo:team-seat-b"]
    meters = {lane["id"]: lane["meters"][0] for lane in result["lanes"]}
    # Explicit synthetic counters per seat; nothing is ever summed or replaced.
    assert (meters["mimo:team-owner"]["allowance"], meters["mimo:team-owner"]["current"]) == (
        1000,
        250,
    )
    assert (meters["mimo:team-seat-b"]["allowance"], meters["mimo:team-seat-b"]["current"]) == (
        2000,
        500,
    )
    assert meters["mimo:plan"]["allowance"] == 82_000_000_000


def test_b_seat_absent_keeps_the_spec_281_shape():
    result = team_report(plan_detail(), plan_usage(), team_sample(), NOW)
    assert [lane["id"] for lane in result["lanes"]] == ["mimo:plan", "mimo:team-owner"]


def test_b_seat_failed_read_still_writes_a_fresh_fail_closed_row():
    result = team_report(plan_detail(), plan_usage(), team_sample(), NOW, None)
    b_row = result["lanes"][2]
    assert b_row["id"] == "mimo:team-seat-b" and b_row["status"] == "unknown"
    assert "meters" not in b_row
    assert result["lanes"][1]["status"] == "ok"  # owner row unaffected


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"seat_status": "PENDING"}, "not usable capacity"),
        ({"total": 0}, "invalid"),
        ({"used": float("nan")}, "invalid"),
        ({"used": -1}, "invalid"),
    ],
)
def test_b_seat_fail_closed_payloads(overrides, reason):
    row = team_seat_b_lane(team_sample_b(**overrides), NOW)
    assert row["id"] == "mimo:team-seat-b" and row["kind"] == "mimo"
    assert row["status"] == "unknown" and "meters" not in row
    assert reason in row["reason"]


def test_b_seat_full_counter_is_exhausted_with_measured_numbers():
    row = team_seat_b_lane(team_sample_b(total=2000, used=2000), NOW)
    assert row["status"] == "exhausted"
    assert row["meters"][0]["current"] == 2000


def test_b_seat_leaks_no_identifiers():
    payload = team_sample_b()
    payload["data"]["projectId"] = "project-b-must-not-leak"
    payload["data"]["seat"]["seatId"] = "seat-b-identifier-must-not-leak"
    dump = json.dumps(team_seat_b_lane(payload, NOW))
    assert "project-b-must-not-leak" not in dump
    assert "seat-b-identifier-must-not-leak" not in dump


def test_view_renders_b_seat_row_with_distinct_identity(tmp_path, monkeypatch):
    combined = team_report(plan_detail(), plan_usage(), team_sample(), NOW, team_sample_b())
    view = _write_mimo_report(tmp_path, monkeypatch, combined["lanes"])
    rows = {row["id"]: row for row in view["lanes"]}
    assert set(rows) == {"mimo:plan", "mimo:team-owner", "mimo:team-seat-b"}
    assert rows["mimo:team-seat-b"]["identity"] == "MiMo Team B"
    assert rows["mimo:team-seat-b"]["status"] == "ok"


def test_view_absent_b_seat_row_preserves_owner_shape(tmp_path, monkeypatch):
    combined = team_report(plan_detail(), plan_usage(), team_sample(), NOW)
    view = _write_mimo_report(tmp_path, monkeypatch, combined["lanes"])
    assert [row["id"] for row in view["lanes"]] == ["mimo:plan", "mimo:team-owner"]


def test_view_ambiguous_b_seat_rows_fail_closed(tmp_path, monkeypatch):
    combined = team_report(plan_detail(), plan_usage(), team_sample(), NOW, team_sample_b())
    b_row = combined["lanes"][2]
    view = _write_mimo_report(
        tmp_path, monkeypatch, [combined["lanes"][0], combined["lanes"][1], b_row, b_row]
    )
    rows = [row for row in view["lanes"] if row["id"] == "mimo:team-seat-b"]
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    assert "ambiguous" in rows[0]["reason"]


def test_view_b_seat_failure_replaces_the_healthy_meter(tmp_path, monkeypatch):
    fresh = team_report(plan_detail(), plan_usage(), team_sample(), NOW, team_sample_b())["lanes"]
    failed = team_report(plan_detail(), plan_usage(), team_sample(), NOW, None)["lanes"]
    view = _write_mimo_report(tmp_path, monkeypatch, fresh)
    assert [r for r in view["lanes"] if r["id"] == "mimo:team-seat-b"][0]["status"] == "ok"
    view = _write_mimo_report(tmp_path, monkeypatch, failed)
    rows = [row for row in view["lanes"] if row["id"] == "mimo:team-seat-b"]
    assert rows[0]["status"] == "unknown" and not rows[0]["meters"]


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


@pytest.mark.parametrize("project", ["../escape", "a/b", "a?x=1", "a#fragment", "x" * 129])
def test_probe_rejects_project_path_before_attaching(monkeypatch, project):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid project reached the browser")

    monkeypatch.setitem(
        sys.modules, "lib", SimpleNamespace(interactive=SimpleNamespace(attach=forbidden))
    )
    monkeypatch.setenv("MIMO_EXPECTED_ACCOUNT_ID", "expected")
    monkeypatch.setenv("MIMO_TEAM_PROJECT_ID", project)
    with pytest.raises(ValueError):
        _probe["main"]()


@pytest.mark.parametrize("identity_matches", [True, False])
def test_probe_explicit_team_read_requires_verified_identity(monkeypatch, capsys, identity_matches):
    calls = []

    class Page:
        def on(self, event, callback):
            self.capture = callback

        def goto(self, *args, **kwargs):
            # Navigation never emits the Team-seat response.
            for path, body in {
                "/api/v1/tokenPlan/detail": plan_detail(),
                "/api/v1/tokenPlan/usage": plan_usage(),
                "/api/v1/userProfile": {
                    "code": 0,
                    "data": {"userId": "expected" if identity_matches else "other"},
                },
            }.items():
                self.capture(
                    SimpleNamespace(
                        url="https://platform.xiaomimimo.com" + path,
                        status=200,
                        json=lambda body=body: body,
                    )
                )

        def evaluate(self, script, path):
            calls.append(path)
            assert "AbortController" in script
            return team_sample()

    @contextmanager
    def attach(*args, **kwargs):
        yield None, None, None, Page()

    monkeypatch.setitem(
        sys.modules, "lib", SimpleNamespace(interactive=SimpleNamespace(attach=attach))
    )
    monkeypatch.setenv("MIMO_EXPECTED_ACCOUNT_ID", "expected")
    monkeypatch.setenv("MIMO_TEAM_PROJECT_ID", "synthetic-project")
    if not identity_matches:
        with pytest.raises(ValueError, match="identity"):
            _probe["main"]()
        assert calls == []
    else:
        assert _probe["main"]() == 0
        assert calls == ["/api/v1/project/synthetic-project/teamTokenPlan/my/seat"]
        result = json.loads(capsys.readouterr().out)
        assert result["lanes"][1]["id"] == "mimo:team-owner"
        assert result["lanes"][1]["meters"][0]["current"] == 250


# --- 290: exhaustion classification + stale/unknown capacity fail-closed -----


def test_report_full_meter_exhausted_regardless_of_reset():
    # Receipt 02/10 23:44 BRT: the live individual plan rendered "ok" at
    # 100.04% with a future reset. A full meter REFUSES — the source row must
    # say so (used >= limit), matching the team seat rule.
    assert sample(82_000_000_000)["lanes"][0]["status"] == "exhausted"
    assert sample(82_000_000_001)["lanes"][0]["status"] == "exhausted"
    assert sample(81_999_999_999)["lanes"][0]["status"] == "ok"


@pytest.mark.parametrize(
    "row,klass",
    [
        ({"status": "stale", "meters": [{"pct": 50}]}, "stale"),
        ({"status": "unknown", "meters": [{"pct": 50}]}, "unknown"),
        ({"status": "exhausted", "meters": []}, "exhausted"),
        ({"status": "ok", "meters": [{"pct": 50}]}, "usable"),
        ({"status": "ok", "meters": []}, "unknown"),
        ({"status": "ok", "meters": [{"pct": 100}]}, "exhausted"),
    ],
)
def test_capacity_fail_closed_for_stale_and_unknown(row, klass):
    # Stale/unknown readings never imply usable capacity; a usable claim needs
    # positive measured evidence, and a full meter refuses even an "ok" verdict.
    from anthropic_throttle_proxy.ui.presentation import row_capacity_class

    assert row_capacity_class(row) == klass
