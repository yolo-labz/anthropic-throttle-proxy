"""Real UI route regressions; synthetic telemetry, no credentials or external I/O."""

import asyncio
import json
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from ui_render import workload_snapshot

from anthropic_throttle_proxy import fleet_ui_config, history
from anthropic_throttle_proxy.ui import routes


class Gauge(HTMLParser):
    def __init__(self):
        super().__init__()
        self.label = ""
        self.poll = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "svg" and attrs.get("class") == "tps-arc":
            self.label = attrs.get("aria-label", "")
        if attrs.get("id") == "stats":
            self.poll = attrs.get("hx-get", "")


@pytest.fixture
async def ui(monkeypatch):
    now = 1000.0
    history.reset()
    monkeypatch.setattr(routes.time, "time", lambda: now)
    monkeypatch.setattr(routes._accounts, "account_view", lambda *args: [])
    monkeypatch.setattr(routes._accounts, "bearer_labels", lambda: {})
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda *args: None)
    monkeypatch.setattr(routes._proxy, "bearer_state", {})
    monkeypatch.setattr(
        routes._fleet.config, "FLEET_HEALTH_URLS", "mimo:http://example.test/health"
    )
    monkeypatch.setattr(routes._copilot.config, "COPILOT_ORGS", "")
    row = workload_snapshot()
    monkeypatch.setattr(routes._fleet, "_cache", {"http://example.test/health": (now, row)})
    monkeypatch.setattr(fleet_ui_config, "load", lambda: {"subscriptions": [], "defaults": {}})
    calls = []

    async def external(*args, **kwargs):
        calls.append("network collector")
        return []

    for module, name in (
        (routes._accounts, "refresh_endpoint"),
        (routes._fleet, "refresh"),
        (routes._copilot, "refresh"),
    ):
        monkeypatch.setattr(module, name, external)
    app = web.Application()
    routes.attach_ui(app)
    # Exercise the real handlers/templates, not the collector's startup loop.
    app.on_startup.clear()
    app.on_cleanup.clear()
    async with TestClient(TestServer(app)) as client:
        yield client, row, calls
    history.reset()


async def test_desktop_producer_renders_independent_weekly_remaining(ui, tmp_path, monkeypatch):
    from anthropic_throttle_proxy import desktop_report

    client, _, calls = ui
    path = tmp_path / "desktop.json"
    desktop_report.publish(
        path,
        desktop_report.report(
            {"percent": 93.6, "resetAt": 3000}, datetime.fromtimestamp(1000, UTC)
        ),
    )
    monkeypatch.setenv("THROTTLE_MIMO_DESKTOP_REPORT", str(path))
    for route in ("/ui", "/ui/stats?source=mimo"):
        response = await client.get(route)
        assert response.status == 200
        html = await response.text()
        desktop = html.split('data-subscription-id="mimo:desktop-subscription"')[1].split("</tr>")[
            0
        ]
        assert "MiMo Desktop" in desktop
        assert "93.6% left" in desktop
        assert "weekly" in desktop
        assert "sampled" in desktop
        assert "credits left" not in desktop
        assert "6%" in desktop
    assert calls == []


async def test_routes_are_network_free_and_select_the_requested_sibling(ui):
    client, _, calls = ui
    response = await client.get("/ui?source=mimo")
    html = await response.text()
    assert response.status == 200
    assert calls == []
    gauge = Gauge()
    gauge.feed(html)
    assert gauge.poll == "/ui/stats?source=mimo"
    assert "mimo output throughput" in html
    assert '<span class="tps-num">120</span>' in html
    assert "120 output tokens per second" in gauge.label
    response = await client.get("/ui/stats?source=mimo")
    assert response.status == 200
    assert '<span class="tps-num">120</span>' in await response.text()
    assert calls == []


@pytest.mark.parametrize("change", ["stale", "missing", "malformed", "failed"])
async def test_bad_workload_never_draws_local_zero_or_old_throughput(ui, change):
    client, row, calls = ui
    if change == "stale":
        routes._fleet._cache["http://example.test/health"] = (900, row)
    elif change == "missing":
        row.pop("throughput")
    elif change == "malformed":
        row["throughput"] = {"bucket_seconds": 10, "tokens": [[True, 2]]}
    else:
        row["ok"] = False
    response = await client.get("/ui/stats?source=mimo")
    html = await response.text()
    assert response.status == 200
    assert "throughput unavailable" in html
    assert '<span class="tps-num">120</span>' not in html
    assert "local output throughput" not in html
    assert calls == []


async def test_unknown_arc_is_accessibly_unknown_not_zero(ui):
    client, _, _ = ui
    response = await client.get("/ui?source=local")
    html = await response.text()
    gauge = Gauge()
    gauge.feed(html)
    assert "unknown" in gauge.label.lower()
    assert "0 output tokens per second" not in gauge.label
    assert 'class="tps-value"' not in html
    assert "not a subscription quota" in html


async def test_sampler_staleness_is_not_erased_by_html_refresh(ui):
    client, _, _ = ui
    history.observe_tokens(out=600)
    history.record(0, 0, 4, now=900)
    response = await client.get("/ui/stats?source=local")
    html = await response.text()
    assert "throughput sample stale" in html
    assert '<span class="tps-num">60</span>' not in html


async def test_partial_keeps_revision_reload_and_default_view_policy(ui, monkeypatch):
    client, _, _ = ui
    response = await client.get("/ui/stats?source=mimo&rev=old")
    assert response.headers.get("HX-Refresh") == "true"
    response = await client.get("/ui/stats?source=mimo&rev=" + routes._ASSET_V)
    assert "HX-Refresh" not in response.headers
    config = {"subscriptions": [], "defaults": {"workload": "mimo"}}
    monkeypatch.setattr(fleet_ui_config, "load", lambda: config)
    response = await client.get("/ui?source=local")
    assert "local output throughput" in await response.text()
    assert config["defaults"]["workload"] == "mimo"  # per-tab choice never writes policy
    response = await client.get("/ui")
    assert "mimo output throughput" in await response.text()


async def test_weekly_desktop_unknown_is_not_monthly_token_plan_usage(ui, monkeypatch, tmp_path):
    client, _, _ = ui
    report = tmp_path / "meters.json"
    report.write_text(
        json.dumps(
            {
                "generatedAt": datetime.fromtimestamp(1000, UTC).isoformat(),
                "intervalSeconds": 900,
                "lanes": [
                    {
                        "id": "mimo:desktop-subscription",
                        "kind": "mimo",
                        "status": "unknown",
                        "identity": "MiMo Desktop subscription",
                        "meters": [{"limitId": "weekly"}],
                    },
                    {
                        "id": "mimo:plan",
                        "kind": "mimo",
                        "status": "ok",
                        "identity": "MiMo Token Plan",
                        "plan": "monthly credits",
                        "meters": [
                            {
                                "limitId": "monthly",
                                "usedPercent": 75,
                                "remaining": 250,
                                "allowance": 1000,
                                "resetsAt": 2000,
                            }
                        ],
                    },
                ],
            }
        )
    )
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(report))
    routes._lanes._cache = None
    response = await client.get("/ui/stats?source=mimo")
    html = await response.text()
    assert 'data-subscription-id="mimo:desktop-subscription"' in html
    assert 'data-subscription-id="mimo:plan"' in html
    desktop = html.split('data-subscription-id="mimo:desktop-subscription"')[1].split("</tr>")[0]
    plan = html.split('data-subscription-id="mimo:plan"')[1].split("</tr>")[0]
    assert "weekly" in desktop and "no reading" in desktop
    assert 'class="pct"' not in desktop  # no producer reading, no invented percentage
    assert "monthly credits" in plan and "75%" in plan
    assert "250 left of 1000" in plan and "resets" in plan
    assert "used of monthly quota" in plan


async def test_background_partial_failure_retries_and_tasks_cancel(monkeypatch):
    calls = []
    second = asyncio.Event()

    async def failed(now):
        calls.append("failed")
        raise RuntimeError("synthetic collector failure")

    async def healthy(now):
        calls.append("healthy")
        if calls.count("healthy") >= 2:
            second.set()
        return []

    monkeypatch.setattr(routes._fleet, "refresh", failed)
    monkeypatch.setattr(routes._copilot, "refresh", healthy)
    monkeypatch.setattr(routes._fleet, "TTL_S", 0.01)
    monkeypatch.setattr(routes._config, "FLEET_HEALTH_URLS", "mimo:http://example.test/health")
    monkeypatch.setattr(routes._config, "ACCOUNT_CRED_PATHS", "")
    app = web.Application()
    await routes._start_account_refresher(app)
    try:
        await asyncio.wait_for(second.wait(), timeout=1)
        assert calls.count("healthy") >= 2
        assert not app["_panel_refresher"].done()
    finally:
        await routes._stop_account_refresher(app)
    assert app["_panel_refresher"].cancelled()
