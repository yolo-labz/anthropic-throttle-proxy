"""Spec 381 — live prompt-cache health panel (in-process counters only)."""

from __future__ import annotations

import inspect
import re

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import proxy, session_affinity
from anthropic_throttle_proxy.ui import cache_health, routes

# Shape of the live 23:11 BRT read (08/11/2026), one model.
FIXTURE = [
    ("claude-opus", "input", 7_330_000),
    ("claude-opus", "cache_read", 1_170_000_000),
    ("claude-opus", "cache_creation", 40_400_000),
    ("claude-opus", "output", 4_740_000),
]


def _row(samples):
    return cache_health.view(samples, 0.0)["rows"][0]


def test_share_math_uses_otel_input_total() -> None:
    r = _row(FIXTURE)
    total = 7_330_000 + 1_170_000_000 + 40_400_000
    assert r["input_total"] == total and r["input_total_exact"]
    assert r["read_share"] == round(100 * 1_170_000_000 / total, 1) == 96.1
    assert r["write_share"] == round(100 * 40_400_000 / total, 1) == 3.3
    assert [s["key"] for s in r["bar"]] == ["cache_read", "cache_write", "fresh_input"]
    assert abs(sum(s["w"] for s in r["bar"]) - 100) < 0.01
    assert r["counts"]["output"] == 4_740_000  # never part of the input total


def test_missing_kind_is_unknown_not_zero() -> None:
    r = _row([("m", "input", 100), ("m", "cache_read", 300), ("m", "output", 5)])
    assert r["counts"]["cache_creation"] is None
    assert r["missing"] == ["cache_creation"]
    assert not r["input_total_exact"] and r["input_total"] == 400
    assert r["write_share"] is None
    assert (r["read_share"], r["read_share_bound"]) == (75.0, "≤ ")
    r = _row([("m", "cache_read", 300)])  # uncached remainder unknown
    assert r["read_share"] is None and r["counts"]["input"] is None


def test_unrelated_kinds_and_models_stay_separate() -> None:
    rows = cache_health.view(FIXTURE + [("glm", "input", 9), ("glm", "bogus", 1)], 0.0)["rows"]
    assert [r["model"] for r in rows] == ["claude-opus", "glm"]
    assert rows[1]["counts"] == {
        "input": 9,
        "cache_read": None,
        "cache_creation": None,
        "output": None,
    }


async def _get(samples, monkeypatch) -> str:
    monkeypatch.setattr(cache_health, "token_samples", lambda: samples)
    app = web.Application()
    routes.attach_ui(app)
    cache_health.attach_cache_health(app)
    app.on_startup.clear()
    app.on_cleanup.clear()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/ui/cache")
        assert resp.status == 200
        return await resp.text()
    finally:
        await client.close()


async def test_zero_counters_render_no_traffic_yet(monkeypatch) -> None:
    session_affinity.reset()
    html = await _get([], monkeypatch)
    assert "no traffic yet" in html and "%" not in html
    assert "this process only" in html and "since gateway start" in html
    assert re.search(r"pins <b>0</b>", html)


async def test_route_renders_shares_unknowns_and_no_dollar(monkeypatch) -> None:
    html = await _get(FIXTURE + [("glm", "input", 9)], monkeypatch)
    assert "96.1%" in html and "3.3%" in html
    assert "1,217,730,000" in html and "4,740,000" in html
    assert "unknown" in html  # glm cache kinds never observed
    assert 'class="u-cache_read"' in html
    assert "$" not in html
    assert "waste" not in html.lower() and "attributed overhead" in html


def test_real_counter_feeds_the_panel() -> None:
    from anthropic_throttle_proxy.metrics import M_TOKENS

    M_TOKENS.labels(model="spec381-probe", kind="cache_read").inc(3)
    try:
        assert ("spec381-probe", "cache_read", 3.0) in cache_health.token_samples()
    finally:
        M_TOKENS.remove("spec381-probe", "cache_read")


def test_health_does_not_build_the_panel() -> None:
    assert "cache_health" not in inspect.getsource(proxy.health)
