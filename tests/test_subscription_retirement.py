"""Explicit display retirement, never inferred from missing quota evidence."""

import copy
import os
from pathlib import Path

import jinja2
import pytest
import yaml

from anthropic_throttle_proxy import fleet_ui_config
from anthropic_throttle_proxy.ui import routes
from anthropic_throttle_proxy.ui.presentation import apply_display


@pytest.mark.parametrize("bad", ["mimo:desktop-subscription", None, 7, [None], [False], [""]])
def test_hidden_subscriptions_rejects_invalid_types(tmp_path, bad):
    path = tmp_path / "invalid.yaml"
    path.write_text(
        yaml.safe_dump({"subscriptions": [], "defaults": {"hidden_subscriptions": bad}})
    )
    loaded = fleet_ui_config.load(path)
    assert "hidden_subscriptions" in loaded["config_error"]


def test_bad_retirement_config_keeps_last_good_inventory(tmp_path):
    path = tmp_path / "inventory.yaml"
    good = {"subscriptions": [], "defaults": {"hidden_subscriptions": ["retired"]}}
    path.write_text(yaml.safe_dump(good))
    assert fleet_ui_config.load(path)["config_error"] is None
    stamp = path.stat().st_mtime_ns
    path.write_text("subscriptions: []\ndefaults:\n  hidden_subscriptions: retired\n")
    os.utime(path, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    loaded = fleet_ui_config.load(path)
    assert loaded["config_error"]
    assert loaded["defaults"]["hidden_subscriptions"] == ["retired"]


def test_explicit_retirement_is_exact_and_does_not_mutate_source(tmp_path):
    path = tmp_path / "inventory.yaml"
    path.write_text("subscriptions: []\ndefaults:\n  hidden_subscriptions: [retired, codex:b]\n")
    view = {
        "subscriptions": [
            {"id": "retired", "family": "chinese-frontier", "status": "stale"},
            {"id": "retired-sibling", "family": "chinese-frontier", "status": "unknown"},
            {"id": "operator-b", "lane_id": "codex:b", "family": "openai"},
            {"id": "paid-but-spent", "status": "exhausted"},
        ]
    }
    before = copy.deepcopy(view)
    out = apply_display(view, fleet_ui_config.load(path))
    assert [r["id"] for r in out["subscriptions"]] == ["retired-sibling", "paid-but-spent"]
    assert view == before
    assert len(apply_display(view, {})["subscriptions"]) == 4


async def test_real_collection_hides_retired_sources_without_erasing_measurements(monkeypatch):
    config = {
        "subscriptions": [],
        "defaults": {
            "hidden_subscriptions": ["mimo:desktop-subscription"],
            "show_primary": False,
        },
    }
    source = {
        "age_s": 5,
        "interval_s": 900,
        "lanes": [
            {"id": "mimo:desktop-subscription", "kind": "mimo", "status": "stale", "meters": []},
            {"id": "mimo:team-b", "kind": "mimo", "status": "unknown", "meters": []},
            {"id": "zai:plan", "kind": "zai", "status": "exhausted", "meters": []},
        ],
    }
    published = []
    monkeypatch.setattr(fleet_ui_config, "load", lambda: config)
    monkeypatch.setattr(routes._lanes, "view", lambda now: source)
    monkeypatch.setattr(routes._accounts, "account_view", lambda *a: [])
    monkeypatch.setattr(routes, "_cached_fleet", lambda now: [])
    monkeypatch.setattr(routes, "_cached_copilot", lambda now: [])
    monkeypatch.setattr(routes, "_publish_lane_gauges", lambda value: published.append(value))
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda *a: None)
    raw = await routes._collect_view(project=False)
    projected = await routes._collect_view()
    assert len(raw["subscriptions"]) == 3
    assert {r["id"] for r in projected["subscriptions"]} == {"mimo:team-b", "zai:plan"}
    assert projected["status"]["verdict"] == "SUBSCRIPTIONS"
    assert projected["summary"]["total"] == 2
    assert all(value is source for value in published)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(routes._TEMPLATES), autoescape=True)
    html = env.get_template("partials/stats.html").render(**projected)
    assert 'data-subscription-id="mimo:desktop-subscription"' not in html
    assert 'data-subscription-id="mimo:team-b"' in html
    assert 'data-subscription-id="zai:plan"' in html


def test_current_board_example_does_not_pin_stale_tiers_or_models():
    path = Path(__file__).parents[1] / "docs/examples/fleet-ui-current.yaml"
    config = fleet_ui_config.load(path)
    assert config["config_error"] is None
    assert config["defaults"]["show_primary"] is False
    assert "mimo:desktop-subscription" in config["defaults"]["hidden_subscriptions"]
    assert all(not s.get("plan") for s in config["subscriptions"])
    assert {s["id"] for s in config["subscriptions"]} == {
        "codex:a",
        "codex:c",
        "zai:plan",
        "copilot:personal",
        "mimo:plan",
        "mimo:team-owner",
        "mimo:team-b",
    }
