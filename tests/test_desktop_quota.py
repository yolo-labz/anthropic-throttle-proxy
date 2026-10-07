"""Desktop quota is remaining percent, never the Token Plan credit allowance."""

import json
from datetime import UTC, datetime

import pytest

from anthropic_throttle_proxy import lanes

NOW = datetime(2026, 10, 7, 21, tzinfo=UTC).timestamp()
LANE_ID = "mimo:desktop-subscription"


@pytest.fixture(autouse=True)
def clear_cache():
    lanes._cache = None
    yield
    lanes._cache = None


def payload():
    return {
        "schema": 1,
        "generatedAt": datetime.fromtimestamp(NOW, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intervalSeconds": 300,
        "lanes": [
            {
                "id": LANE_ID,
                "kind": "mimo",
                "status": "ok",
                "plan": "Desktop subscription · weekly quota",
                "meters": [
                    {
                        "limitId": "weekly",
                        "usedPercent": 6.4,
                        "remainingPercent": 93.6,
                        "unit": "percent",
                        "resetsAt": NOW + 86400,
                    }
                ],
            }
        ],
    }


def read(tmp_path, monkeypatch, raw, now=NOW):
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps(
            {
                **payload(),
                "lanes": [
                    {
                        "id": "mimo:plan",
                        "kind": "mimo",
                        "status": "ok",
                        "meters": [{"limitId": "monthly", "usedPercent": 98}],
                    }
                ],
            }
        )
    )
    desktop = tmp_path / "desktop.json"
    desktop.write_text(json.dumps(raw))
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(base))
    monkeypatch.setenv("THROTTLE_MIMO_DESKTOP_REPORT", str(desktop))
    monkeypatch.delenv("THROTTLE_MIMO_REPORT", raising=False)
    return lanes.view(now)["lanes"]


def test_independent_weekly_units_and_no_inferred_pace(tmp_path, monkeypatch):
    rows = read(tmp_path, monkeypatch, payload())
    assert {row["id"] for row in rows} == {LANE_ID, "mimo:plan"}
    row = next(row for row in rows if row["id"] == LANE_ID)
    assert row["status"] == "ok"
    meter = row["meters"][0]
    assert meter["used_pct"] == pytest.approx(6.4)
    assert meter["note"] == "93.6% weekly remaining"
    assert meter["allowance"] is None and meter["remaining"] == "93.6%"
    assert meter["window_mins"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "missing",
        "monthly",
        "unit",
        "range",
        "inconsistent",
        "clock",
        "interval",
        "schema",
        "status",
        "future",
    ],
)
def test_bad_contract_unknown_not_healthy(tmp_path, monkeypatch, mutation):
    raw = payload()
    meter = raw["lanes"][0]["meters"][0]
    if mutation == "duplicate":
        raw["lanes"] *= 2
    if mutation == "missing":
        raw["lanes"] = []
    if mutation == "monthly":
        meter["limitId"] = "monthly"
    if mutation == "unit":
        meter["unit"] = "credits"
    if mutation == "range":
        meter["remainingPercent"] = 101
    if mutation == "inconsistent":
        meter["usedPercent"] = 93.6
    if mutation == "clock":
        raw["generatedAt"] = "bad"
    if mutation == "interval":
        raw.pop("intervalSeconds")
    if mutation == "schema":
        raw["schema"] = 2
    if mutation == "status":
        raw["lanes"][0]["status"] = []
    if mutation == "future":
        raw["generatedAt"] = datetime.fromtimestamp(NOW + 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = next(row for row in read(tmp_path, monkeypatch, raw) if row["id"] == LANE_ID)
    assert row["status"] == "unknown"
    assert row["binding_pct"] is None


def test_desktop_ages_independently(tmp_path, monkeypatch):
    rows = read(tmp_path, monkeypatch, payload(), NOW + 601)
    row = next(row for row in rows if row["id"] == LANE_ID)
    assert row["status"] == "stale"
    assert "last sample" in row["reason"]


def test_missing_file_replaces_previously_fresh_row(tmp_path, monkeypatch):
    read(tmp_path, monkeypatch, payload())
    (tmp_path / "desktop.json").unlink()
    rows = lanes.view(NOW + lanes.TTL_S)["lanes"]
    row = next(row for row in rows if row["id"] == LANE_ID)
    assert row["status"] == "unknown"
    assert not row["meters"]
