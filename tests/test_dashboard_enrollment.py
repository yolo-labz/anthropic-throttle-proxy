"""Enrollment acceptance: synthetic metadata, real catalogue and template paths."""

import copy
import json

import pytest
from ui_render import render_stats

from anthropic_throttle_proxy import fleet_ui_config as catalogue
from anthropic_throttle_proxy.ui import routes
from anthropic_throttle_proxy.ui.presentation import (
    apply_display,
    attach_provider_capacity,
    capacity_summary,
)


def registry(tmp_path, meters=None):
    path = tmp_path / "subscription-lanes.json"
    path.write_text(json.dumps({"schema": 1, "lanes": {"codex": {"meters": meters or []}}}))
    return path


def test_native_enrollment_reads_only_declared_meter_ids(tmp_path):
    path = registry(tmp_path, ["codex:a", "codex:c"])
    assert catalogue.native_codex_meters(path) == frozenset({"codex:a", "codex:c"})
    assert catalogue.native_codex_meters(registry(tmp_path)) == frozenset()


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '["codex:b"]',
        '{"schema":true,"lanes":{}}',
        '{"schema":1,"lanes":{"codex":{"meters":"codex:b"}}}',
        '{"schema":1,"lanes":{"codex":{"meters":[true]}}}',
        '{"schema":1,"lanes":{"codex":{"meters":["codex:a","codex:a"]}}}',
        '{"schema":1,"schema":2,"lanes":{}}',
        " " * 16385,
    ],
)
def test_invalid_native_registry_never_claims_non_enrollment(tmp_path, raw):
    path = tmp_path / "invalid.json"
    path.write_text(raw)
    assert catalogue.native_codex_meters(path) is None


def test_missing_registry_and_nonregular_source_remain_unknown(tmp_path):
    assert catalogue.native_codex_meters(tmp_path / "missing") is None
    assert catalogue.native_codex_meters(tmp_path) is None


def test_invalidated_registry_drops_membership_instead_of_reusing_last_good(tmp_path):
    path = registry(tmp_path, ["codex:a"])
    assert catalogue.native_codex_meters(path) == frozenset({"codex:a"})
    path.write_text("invalid")
    assert catalogue.native_codex_meters(path) is None


def test_source_clocks_survive_row_building_independently():
    rows = routes._build_subscriptions(
        [],
        {
            "lanes": [
                {
                    "id": "codex:a",
                    "kind": "codex",
                    "status": "stale",
                    "sample_age_s": 1900,
                    "sample_observed_at": "08/10/2026 13:00 UTC",
                    "sample_interval_s": 900,
                },
                {
                    "id": "mimo:desktop-subscription",
                    "kind": "mimo",
                    "status": "ok",
                    "sample_age_s": 60,
                    "sample_observed_at": "08/10/2026 13:30 UTC",
                    "sample_interval_s": 60,
                },
            ]
        },
        1791470000,
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["codex:a"]["sample_age_s"] == 1900
    assert by_id["codex:a"]["src"] == "Local meter report"
    assert by_id["mimo:desktop-subscription"]["sample_age_s"] == 60
    assert by_id["mimo:desktop-subscription"]["src"] == "Desktop weekly report"
    html = render_stats(subscriptions=rows)
    assert "Source sample 1900s old · 08/10/2026 13:00 UTC · every 900s" in html
    assert "Source sample 60s old · 08/10/2026 13:30 UTC · every 60s" in html


def test_unenrolled_and_unmatched_catalogue_do_not_count_as_active_capacity(tmp_path):
    rows = [{"id": "codex:b", "status": "ok", "meters": [{"pct": 0}]}]
    config = {
        "subscriptions": [
            {"id": "b-caption", "lane": "lane:codex:b", "label": "Codex B", "family": "openai"},
            {"id": "unassigned", "label": "Unassigned seat", "family": "chinese-frontier"},
            {"id": "a-caption", "lane": "codex:a", "label": "Codex A", "family": "openai"},
        ]
    }
    original = copy.deepcopy((rows, config))
    decorated = catalogue.decorate(rows, config, native_meters=frozenset({"codex:a"}))["rows"]
    view = apply_display({"subscriptions": decorated}, {})
    summary = capacity_summary(view)
    by_id = {row["id"]: row for row in view["subscriptions"]}
    assert by_id["codex:b"]["enrollment"] == "not enrolled"
    assert by_id["codex:b"]["status"] == "not enrolled"
    assert by_id["codex:b"]["meters"] == [{"pct": 0, "icon": "📊", "reset_at": ""}]
    assert summary["total"] == 1 and summary["catalogue_total"] == 2
    assert summary["counts"]["unknown"] == 1 and summary["counts"]["usable"] == 0
    providers = [{"name": "codex"}]
    attach_provider_capacity(providers, [by_id["codex:b"]])
    assert providers[0]["capacity"]["state"] == "unmeasured"
    attach_provider_capacity(providers, view["subscriptions"])
    assert providers[0]["capacity"]["state"] == "unknown"
    assert providers[0]["capacity"]["matched"] == ["a-caption"]
    assert (rows, config) == original


async def test_collection_keeps_local_refusal_separate_from_mimo_capacity(monkeypatch, tmp_path):
    monkeypatch.setenv("THROTTLE_LANE_REGISTRY_FILE", str(registry(tmp_path, ["codex:a"])))
    monkeypatch.setattr(routes._accounts, "account_view", lambda *_: [])
    monkeypatch.setattr(routes, "_cached_fleet", lambda *_: [])
    monkeypatch.setattr(routes, "_cached_copilot", lambda *_: [])
    monkeypatch.setattr(
        routes,
        "_compute_status",
        lambda *_args, **_kw: {
            "scope": "local",
            "level": "crit",
            "verdict": "CRIT",
            "binding": None,
            "detail": "local credential refused",
            "since": "",
        },
    )
    monkeypatch.setattr(
        routes._lanes,
        "view",
        lambda *_: {
            **routes._lanes.EMPTY,
            "lanes": [
                {
                    "id": "mimo:desktop-subscription",
                    "kind": "mimo",
                    "family": "chinese-frontier",
                    "status": "ok",
                    "meters": [{"label": "weekly", "used_pct": 29.5, "remaining": "70.5%"}],
                }
            ],
        },
    )
    monkeypatch.setattr(
        catalogue,
        "load",
        lambda: {
            "subscriptions": [
                {"id": "b-caption", "lane": "codex:b", "label": "Codex B", "family": "openai"},
            ]
        },
    )
    view = await routes._collect_view()
    assert view["summary"]["total"] == 1
    assert view["summary"]["counts"]["usable"] == 1
    assert view["summary"]["catalogue_total"] == 1
    html = render_stats(**view)
    assert 'aria-label="Local proxy verdict"' in html
    assert "Local proxy" in html and "CRIT" in html
    assert "Fleet verdict" not in html
    assert "not enrolled" in html
    assert "Catalogue only" in html
    assert "70.5% left" in html
