"""Tests for the fleet strip (sibling-proxy health cross-fetch)."""

from __future__ import annotations

import asyncio

import pytest

from anthropic_throttle_proxy import config, fleet


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    fleet._cache.clear()
    fleet._locks.clear()
    monkeypatch.setattr(config, "FLEET_HEALTH_URLS", "")
    yield
    fleet._cache.clear()
    fleet._locks.clear()


def test_parse_spec_pairs_label_and_url():
    assert fleet.parse_spec("z.ai:http://127.0.0.1:8766/__throttle/health") == [
        ("z.ai", "http://127.0.0.1:8766/__throttle/health")
    ]


def test_parse_spec_multiple_respects_order_and_dedupes():
    raw = "a:http://h:1/__throttle/health,b:http://h:2/__throttle/health,a:http://h:3/__throttle/health"
    assert fleet.parse_spec(raw) == [
        ("a", "http://h:1/__throttle/health"),
        ("b", "http://h:2/__throttle/health"),
    ]


def test_parse_spec_skips_malformed():
    assert fleet.parse_spec("no-colon,, :only, x :") == []
    assert fleet.parse_spec("") == []


def test_parse_health_flattens_stable_fields():
    body = {
        "inflight": 2,
        "queued": 4,
        "served": 644,
        "max_concurrent": 2,
        "queue_mode": "fair",
        "upstream": "https://api.z.ai/api/coding/paas/v4",
        "upstream_egress_ok": True,
        "client_disconnects": 3,
        "upstream_retries": 0,
        "bearers": {"ignored": "nested tree stays on the sibling"},
    }
    view = fleet._parse_health(body)
    assert view["ok"] is True
    assert view["inflight"] == 2 and view["queued"] == 4 and view["served"] == 644
    assert view["upstream_egress_ok"] is True
    assert "bearers" not in view  # the heavy per-bearer tree is dropped


def test_parse_health_non_dict_is_not_ok():
    assert fleet._parse_health([])["ok"] is False


async def test_refresh_empty_when_unset():
    assert await fleet.refresh(0.0) == []


async def test_refresh_returns_env_order_and_marks_down_sibling(monkeypatch):
    monkeypatch.setattr(
        config,
        "FLEET_HEALTH_URLS",
        "up:http://h:1/__throttle/health,down:http://h:2/__throttle/health",
    )

    async def fake(url):
        if "h:1" in url:
            return 200, {"inflight": 1, "served": 5, "upstream_egress_ok": True}
        return 0, None  # transport error / down

    monkeypatch.setattr(fleet, "_fetch_json", fake)
    rows = await fleet.refresh(0.0)
    assert [r["name"] for r in rows] == ["up", "down"]
    assert rows[0]["ok"] is True and rows[0]["served"] == 5
    assert rows[1]["ok"] is False and rows[1]["status"] == 0


async def test_refresh_ttl_caches_within_window(monkeypatch):
    monkeypatch.setattr(config, "FLEET_HEALTH_URLS", "z:http://h/__throttle/health")
    calls = 0

    async def fake(url):
        nonlocal calls
        calls += 1
        return 200, {"inflight": 0}

    monkeypatch.setattr(fleet, "_fetch_json", fake)
    await fleet.refresh(0.0)
    await fleet.refresh(0.0)  # within TTL → no new fetch
    assert calls == 1
    # past TTL → one new fetch
    await fleet.refresh(fleet.TTL_S + 1)
    assert calls == 2


async def test_refresh_http_non_200_is_not_ok(monkeypatch):
    monkeypatch.setattr(config, "FLEET_HEALTH_URLS", "z:http://h/__throttle/health")
    monkeypatch.setattr(fleet, "_fetch_json", lambda url: asyncio.sleep(0, result=(503, None)))
    rows = await fleet.refresh(0.0)
    assert rows[0]["ok"] is False
    assert rows[0]["status"] == 503


async def test_refresh_200_non_json_is_not_ok(monkeypatch):
    """A 200 with a non-dict body (HTML error page, empty body) must not raise
    into the render path — the whole dashboard must not blank on one bad sibling.
    """
    monkeypatch.setattr(config, "FLEET_HEALTH_URLS", "z:http://h/__throttle/health")
    monkeypatch.setattr(fleet, "_fetch_json", lambda url: asyncio.sleep(0, result=(200, "<html>")))
    rows = await fleet.refresh(0.0)
    assert rows[0]["ok"] is False
    assert "non-json" in rows[0]["err"]


def test_parse_health_coerces_type_drift():
    """A sibling returning null/string values coerces to 0, never raises."""
    view = fleet._parse_health({"inflight": None, "queued": "4", "served": None})
    assert view["ok"] is True
    assert view["inflight"] == 0 and view["queued"] == 4


async def test_concurrent_refresh_single_flights(monkeypatch):
    """Two concurrent refresh() calls collapse to one _fetch_json (single-flight)."""
    monkeypatch.setattr(config, "FLEET_HEALTH_URLS", "z:http://h/__throttle/health")
    calls = 0

    async def slow(url):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return 200, {"inflight": 0}

    monkeypatch.setattr(fleet, "_fetch_json", slow)
    await asyncio.gather(fleet.refresh(0.0), fleet.refresh(0.0))
    assert calls == 1
