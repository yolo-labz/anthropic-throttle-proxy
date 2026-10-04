"""The dashboard must not substitute its disabled primary for a busy sibling."""

import asyncio
from types import SimpleNamespace

import pytest
from ui_render import render_stats

from anthropic_throttle_proxy import fleet, fleet_ui_config, history
from anthropic_throttle_proxy.ui import routes, signals


@pytest.fixture
def workload(monkeypatch):
    row = {
        "name": "mimo",
        **fleet._parse_health(
            {
                "inflight": 12,
                "queued": 2,
                "served": 5000,
                "max_concurrent": 20,
                "upstream": "https://example.test",
                "upstream_egress_ok": True,
                "throughput": {"bucket_seconds": 10, "tokens": [[1200, 3000]] * 6},
            }
        ),
    }
    monkeypatch.setattr(
        routes._accounts, "refresh_endpoint", lambda now: asyncio.sleep(0, result={})
    )
    monkeypatch.setattr(routes._accounts, "account_view", lambda *args: [])
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda *args: None)
    monkeypatch.setattr(routes._fleet, "refresh", lambda now: asyncio.sleep(0, result=[row]))
    monkeypatch.setattr(routes._copilot, "refresh", lambda now: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(routes._lanes, "view", lambda now: {"lanes": [], "registry": []})
    monkeypatch.setattr(
        fleet_ui_config,
        "load",
        lambda: {
            "subscriptions": [],
            "defaults": {"show_primary": True, "workload": "mimo"},
        },
    )
    return row


async def test_selected_workload_replaces_local_zero_and_refusal(workload):
    view = await routes._collect_view()
    assert view["inflight"] == 12
    assert view["served"] == 5000
    assert view["tps"].value == 120
    assert view["tps"].tok_in_now == 300
    assert view["show_local"] is False
    assert view["signals"] == []  # never show the idle primary's golden signals
    assert view["bearers"] == []
    assert view["summary"]["live"]["inflight"] == 12
    assert view["status"]["verdict"] == "MIMO WORKLOAD"
    html = render_stats(**view)
    assert "mimo output throughput" in html
    assert '<span class="tps-num">120</span>' in html
    raw = await routes._collect_view(project=False)
    assert "workload_label" not in raw  # display policy cannot rewrite raw readers


async def test_old_runtime_is_unmeasured_not_local_zero(workload):
    workload.pop("throughput", None)
    view = await routes._collect_view()
    assert view["tps"] is None
    assert view["summary"]["throughput"] is None
    assert "throughput unavailable" in render_stats(**view)


async def test_unreachable_workload_never_falls_back_to_primary(workload):
    workload.update(ok=False, status=0)
    view = await routes._collect_view()
    assert view["tps"] is None
    assert view["summary"]["live"] is None
    assert view["status"]["verdict"] == "WORKLOAD UNKNOWN"


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {},
        {"bucket_seconds": 5, "tokens": [[10, 1]]},
        {"bucket_seconds": 10, "tokens": [[True, 2]]},
        {"bucket_seconds": 10, "tokens": [[-1, 2]]},
        {"bucket_seconds": 10, "tokens": [[10, float("inf")]]},
        {"bucket_seconds": 10, "tokens": [[10, 1]] * 361},
    ],
)
async def test_bad_telemetry_is_not_a_render_error(workload, snapshot):
    workload["throughput"] = snapshot
    view = await routes._collect_view()
    assert view["tps"] is None
    assert "throughput unavailable" in render_stats(**view)


def test_workload_config_survives_loading(tmp_path):
    path = tmp_path / "fleet.yaml"
    path.write_text("defaults:\n  workload: mimo\nsubscriptions: []\n")
    assert fleet_ui_config.load(path)["defaults"]["workload"] == "mimo"


@pytest.mark.parametrize("value", [False, 42, "", []])
def test_invalid_workload_config_is_rejected(value):
    with pytest.raises(ValueError, match="workload"):
        fleet_ui_config._validate({"defaults": {"workload": value}, "subscriptions": []})


async def test_health_exports_real_token_history(monkeypatch):
    history.reset()
    try:
        history.observe_tokens(out=1200, in_=3000)
        history.record(queued=0, inflight=1, cap=20)
        response = await routes._proxy.health(SimpleNamespace(query={"telemetry": "1"}))
        import json

        snapshot = json.loads(response.text)["throughput"]
        # Schema-compatible export (strengthened, not weakened): the published
        # two-column buckets stay exactly as contracted, and the measured fresh
        # side arrives as an ADDITIVE sidecar — 0 here is a measured zero.
        assert snapshot == {
            "bucket_seconds": 10,
            "tokens": [[1200, 3000]],
            "tokens_fresh": [0],
        }
        gauge = signals.remote_tps(snapshot)
        assert gauge.value == 120
        assert gauge.tok_in_now == 300.0
        assert gauge.tok_in_fresh == 0.0  # measured zero survives the round trip
        # Legacy SIBLING two-column coverage: no sidecar -> fresh UNKNOWN, and
        # the total side keeps working first-class.
        legacy = {"bucket_seconds": 10, "tokens": [[1200, 3000]]}
        legacy_gauge = signals.remote_tps(legacy)
        assert legacy_gauge.value == 120
        assert legacy_gauge.tok_in_fresh is None
        # The published bucket schema is strict: in-bucket third columns are
        # not a shape this contract accepts.
        assert signals.remote_tps({"bucket_seconds": 10, "tokens": [[1200, 3000, 0]]}) is None
    finally:
        history.reset()
