"""Freshness and display projection regressions; all evidence is synthetic."""

import copy
import json

import pytest

from anthropic_throttle_proxy import lanes
from anthropic_throttle_proxy.ui import routes
from anthropic_throttle_proxy.ui.presentation import _reset_at, apply_display


@pytest.mark.parametrize("interval", [0, -1, True, float("nan"), float("inf"), "900"])
def test_invalid_cadence_cannot_certify_current_capacity(tmp_path, monkeypatch, interval):
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "generatedAt": "2026-01-01T00:00:00Z",
                "intervalSeconds": interval,
                "lanes": [
                    {"id": "codex:a", "kind": "codex", "status": "ok"},
                    {"id": "codex:b", "kind": "codex", "status": "refused"},
                ],
            }
        )
    )
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(path))
    lanes._cache = None
    view = lanes.view(1767225630)
    assert view["interval_s"] is None and view["next_sample_in_s"] is None
    assert [row["status"] for row in view["lanes"]] == ["unknown", "refused"]
    assert "interval invalid" in view["error"]


def test_cadence_and_source_time_are_independent_of_html_polling(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "generatedAt": "2026-01-01T00:00:00Z",
                "intervalSeconds": 900,
                "lanes": [],
            }
        )
    )
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(path))
    lanes._cache = None
    view = lanes.view(1767225630)
    assert view["age_s"] == 30 and view["interval_s"] == 900
    assert view["observed_at"] == "01/01/2026 00:00 UTC"
    assert view["next_sample_in_s"] == 870 and not view["stale"]


@pytest.mark.parametrize("value", [None, True, "bad", float("nan"), float("inf"), 1e300])
def test_bad_reset_timestamp_never_crashes_rendering(value):
    assert _reset_at(value) == ""


def test_pool_scope_duration_and_reset_survive_projection():
    source = {
        "lanes": [
            {
                "id": "codex:c",
                "kind": "codex",
                "status": "exhausted",
                "meters": [
                    {
                        "label": "pool-one",
                        "used_pct": 100,
                        "window_mins": 10080,
                        "resets_at": 1767225600,
                        "reset_in": "1h",
                    },
                    {"label": "pool-two", "used_pct": 0, "window_mins": 300},
                ],
            }
        ]
    }
    original = copy.deepcopy(source)
    rows = routes._build_subscriptions([], source, 1767225500)
    row = apply_display({"subscriptions": rows}, {})["subscriptions"][0]
    assert row["status"] == "pool limited" and "applicability unknown" in row["detail"]
    assert [m["icon"] for m in row["meters"]] == ["📅", "⏱️"]
    assert row["meters"][0]["reset_at"] == "01/01/2026 00:00 UTC"
    assert source == original
    for meter, label in zip(rows[0]["meters"], ("5h", "7d"), strict=True):
        meter["label"] = label
    assert apply_display({"subscriptions": rows}, {})["subscriptions"][0]["status"] == "exhausted"


def test_default_visibility_preserves_primary_and_unknown_meter():
    view = {
        "providers": [{"kind": "primary"}],
        "bearers": [{"id": "x"}],
        "status": {"verdict": "IDLE"},
        "subscriptions": [{"id": "unknown", "meters": [{"label": "raw-pool"}]}],
    }
    before = copy.deepcopy(view)
    displayed = apply_display(view, {})
    assert displayed["show_local"] is True
    assert displayed["providers"] == view["providers"]
    assert displayed["subscriptions"][0]["meters"][0]["label"] == "raw-pool"
    assert displayed["subscriptions"][0]["meters"][0]["icon"] == "📊"
    assert view == before


async def test_hidden_anthropic_does_not_poll_account_endpoint(monkeypatch):
    from anthropic_throttle_proxy import fleet_ui_config

    async def forbidden(*args, **kwargs):
        raise AssertionError("hidden accounts must not be polled for display")

    monkeypatch.setattr(routes._accounts, "refresh_endpoint", forbidden)
    monkeypatch.setattr(
        fleet_ui_config,
        "load",
        lambda: {
            "defaults": {"hidden_families": ["anthropic"], "show_primary": False},
        },
    )
    result = await routes._collect_view()
    assert result["show_local"] is False and result["bearers"] == []
    assert result["status"]["verdict"] == "SUBSCRIPTIONS"
    knobs = routes._config.knob_snapshot()
    assert result["config_count"] == len(knobs)
    assert result["override_count"] == sum(k["override"] for k in knobs)
