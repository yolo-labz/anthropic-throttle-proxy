"""MiMo telemetry: measured monthly credits, independent freshness, no credentials."""

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anthropic_throttle_proxy import lanes
from anthropic_throttle_proxy.ui.routes import _build_subscriptions

report = runpy.run_path(str(Path(__file__).parents[1] / "scripts/mimo-token-plan-probe.py"))[
    "report"
]
NOW = datetime(2026, 9, 23, 16, tzinfo=UTC)


def sample(used=41_000_000_000, limit=82_000_000_000):
    return report(
        {
            "code": 0,
            "data": {
                "planName": "Max",
                "expired": False,
                "currentPeriodEnd": "2026-10-23 23:59:59",
                "apiKey": "must-not-escape",
            },
        },
        {
            "code": 0,
            "data": {
                "monthUsage": {
                    "items": [
                        {"name": "month_total_token", "used": used, "limit": limit, "percent": 0}
                    ]
                }
            },
        },
        NOW,
    )


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
