"""Provider-board visibility is explicit presentation policy, never retirement inference."""

from copy import deepcopy

import jinja2
import pytest

from anthropic_throttle_proxy import fleet_ui_config
from anthropic_throttle_proxy.ui import routes
from anthropic_throttle_proxy.ui.presentation import apply_display, apply_workload


@pytest.mark.parametrize("bad", [None, "mimo", [False], [""]])
def test_hidden_providers_validation(bad):
    with pytest.raises(ValueError, match="hidden_providers"):
        fleet_ui_config._validate({"subscriptions": [], "defaults": {"hidden_providers": bad}})


def test_projection_hides_exact_destinations_not_sibling_accounts():
    view = {
        "subscriptions": [
            {"id": "claude-current", "family": "anthropic", "status": "locked"},
            {"id": "codex:a", "family": "openai", "status": "ok"},
            {"id": "codex:c", "family": "openai", "status": "not enrolled"},
            {"id": "copilot:personal", "family": "github", "status": "exhausted"},
            {"id": "mimo:plan", "family": "chinese-frontier", "status": "stale"},
        ],
        "providers": [{"name": n, "kind": "sibling"} for n in ("mimo", "codex", "future")],
        "lanes": {"registry": [{"id": n} for n in ("mimo", "codex", "future")]},
        "workload_choices": ["mimo", "codex", "future"],
    }
    before = deepcopy(view)
    config = {
        "defaults": {
            "hidden_subscriptions": ["mimo:plan", "codex:c"],
            "hidden_providers": [" MiMo "],
        }
    }
    out = apply_display(view, config)
    assert [r["id"] for r in out["subscriptions"]] == [
        "claude-current",
        "codex:a",
        "copilot:personal",
    ]
    assert [r["name"] for r in out["providers"]] == ["codex", "future"]
    assert [r["id"] for r in out["lanes"]["registry"]] == ["codex", "future"]
    assert out["workload_choices"] == ["codex", "future"]
    assert view == before
    legacy = apply_display(view, {})
    assert len(legacy["providers"]) == 3
    assert len(legacy["subscriptions"]) == 5
    apply_workload(
        out,
        {"defaults": {**config["defaults"], "workload": "mimo"}},
        [{"name": "mimo", "ok": True, "served": 999}],
    )
    assert not out.get("workload_available")
    assert out.get("served") != 999


async def test_current_sources_survive_full_page_and_poll_without_ghosts(monkeypatch):
    config = {
        "subscriptions": [],
        "defaults": {
            "show_primary": False,
            "hidden_subscriptions": ["mimo:plan", "zai:plan"],
            "hidden_providers": ["mimo", "opencode", "opencode-zen", "zai"],
        },
    }
    source = {
        "age_s": 1,
        "interval_s": 900,
        "lanes": [
            {"id": "codex:a", "kind": "codex", "family": "openai", "status": "ok", "meters": []},
            {
                "id": "copilot:personal",
                "kind": "copilot",
                "family": "github",
                "status": "exhausted",
                "meters": [],
            },
            {"id": "mimo:plan", "kind": "mimo", "status": "stale", "meters": []},
            {"id": "zai:plan", "kind": "zai", "status": "refused", "meters": []},
        ],
        "registry": [{"id": n, "provider": n} for n in ("mimo", "zai", "codex", "future")],
    }
    fleet = [{"name": n, "ok": True} for n in ("mimo", "opencode", "opencode-zen", "zai", "future")]
    accounts = [{"bearer_id": "synthetic-current", "email": "current@example.invalid", "ok": True}]
    published = []
    monkeypatch.setattr(fleet_ui_config, "load", lambda: config)
    monkeypatch.setattr(fleet_ui_config, "native_codex_meters", lambda: frozenset({"codex:a"}))
    monkeypatch.setattr(routes._lanes, "view", lambda now: source)
    monkeypatch.setattr(routes._accounts, "account_view", lambda *a: accounts)
    monkeypatch.setattr(routes, "_cached_fleet", lambda now: fleet)
    monkeypatch.setattr(routes, "_cached_copilot", lambda now: [])
    monkeypatch.setattr(routes, "_publish_lane_gauges", lambda value: published.append(value))
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda *a: None)
    before = deepcopy(source)
    raw = await routes._collect_view(project=False)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(routes._TEMPLATES), autoescape=True)
    for template in ("dashboard.html", "partials/stats.html", "partials/stats.html"):
        view = await routes._collect_view()
        assert {r["family"] for r in view["subscriptions"]} >= {"anthropic", "openai"}
        assert "copilot:personal" in {r["id"] for r in view["subscriptions"]}
        assert {r["name"] for r in view["providers"]} == {"future"}
        assert view["workload_choices"] == ["future"]
        assert {r["id"] for r in view["lanes"]["registry"]} == {"codex", "future"}
        html = env.get_template(template).render(**view, asset_v=routes._ASSET_V)
        assert 'data-subscription-id="codex:a"' in html
        assert 'data-subscription-id="copilot:personal"' in html
        assert 'data-subscription-id="mimo:plan"' not in html
        assert 'data-subscription-id="zai:plan"' not in html
    assert len(raw["providers"]) == 6
    assert len(raw["subscriptions"]) == 5
    assert all(value is source for value in published)
    assert source == before
