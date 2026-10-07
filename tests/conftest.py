"""Suite-wide isolation for process-global proxy registries."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from anthropic_throttle_proxy import config, fleet_ui_config, forwarding, limiter, pacing, proxy


@pytest.fixture
def proxy_admission_state(monkeypatch):
    """Shared isolation for HTTP admission tests; never use another test's pools."""
    limiter.set_lock(asyncio.Lock())
    pacing.set_lock(asyncio.Lock())
    config.bearer_limiters.clear()
    config.bearer_state.clear()
    monkeypatch.setitem(config.state, "inflight", 0)
    monkeypatch.setitem(config.state, "queued", 0)
    yield
    config.bearer_limiters.clear()
    config.bearer_state.clear()


@pytest.fixture
def proxy_admission_app(proxy_admission_state):
    """Real admission handler, without background probes or dashboard tasks."""
    app = web.Application()
    app.on_response_prepare.append(forwarding.stamp_proxy_marker)
    app.router.add_route("*", "/{path:.*}", proxy.handler)
    return app


@pytest.fixture(autouse=True)
def _isolate_fleet_ui_config(monkeypatch, tmp_path):
    """Never render the operator's YAML in a test."""
    path = tmp_path / "test-fleet-ui.yaml"
    path.write_text("subscriptions: []\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_UI_CONFIG", str(path))
    fleet_ui_config.reset_cache()
    yield
    fleet_ui_config.reset_cache()


@pytest.fixture(autouse=True)
def _isolate_retry_probe_gates():
    limiter._reset_retry_probe_gates()
    yield
    limiter._reset_retry_probe_gates()


@pytest.fixture(autouse=True)
def _isolate_lane_report(monkeypatch, tmp_path_factory):
    """Point the lane reader at a path that does not exist.

    Otherwise every UI test reads the developer's OWN
    $XDG_RUNTIME_DIR/throttle-lanes.json and asserts against whatever their
    Codex meters happen to say today.
    """
    absent = tmp_path_factory.mktemp("lanes") / "absent.json"
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(absent))
    monkeypatch.delenv("THROTTLE_MIMO_DESKTOP_REPORT", raising=False)
    from anthropic_throttle_proxy import lanes

    lanes._cache = None
    yield
    lanes._cache = None


@pytest.fixture
def meter_rows():
    """Synthetic independently scoped quota rows for endpoint/dispatch acceptance."""

    def rows(owner="exhausted", secondary="ok"):
        return [
            {
                "id": lane,
                "kind": "mimo",
                "status": status,
                "meters": [{"used_pct": 100.0 if status == "exhausted" else 25.0}],
            }
            for lane, status in (("mimo:plan", owner), ("mimo:team-b", secondary))
        ]

    return rows
