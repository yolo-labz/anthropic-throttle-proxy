"""The Observatory's source, scale, uncertainty, route, and safety contract.

Synthetic source: specs/380-usage-panel/usage-summary.v1.example.json.
No live provider calls, account refreshers, timers, or credential reads.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jinja2
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import usage_summary as us
from anthropic_throttle_proxy.ui import auth, routes, usage
from anthropic_throttle_proxy.ui import observatory as ob

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "specs/380-usage-panel/usage-summary.v1.example.json"


def document() -> dict:
    return json.loads(FIXTURE.read_text())


def summary() -> us.Summary:
    return us.parse(document())


def projected(query=None, doc=None, now=None) -> dict:
    source = us.parse(document() if doc is None else doc)
    clock = source.generated_at.timestamp() + 30 if now is None else now
    return ob.build_view(source, query or {}, now=clock)


def rendered(view: dict) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(routes._TEMPLATES)),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template("observatory.html").render(
        ob=view, measures=ob.MEASURES, roles=ob.ROLES, columns=ob.COLUMNS, asset_v="test"
    )


def models(view: dict) -> list[dict]:
    return [m for provider in view["providers"] for m in provider["models"]]


def test_one_source_row_set_drives_totals_geometry_and_exact_table():
    view = projected({"provider": "anthropic", "role": "turn"})
    expected = [r for r in summary().rows if r.provider == "anthropic" and r.role == "turn"]
    assert view["matched_rows"] == len(view["table_rows"]) == 2
    assert view["total"]["value"] == sum(r.counts[k] for r in expected for k in us.ADDITIVE_CLASSES)
    assert sum(m["metric"]["value"] for m in models(view)) == view["total"]["value"]
    assert sum(s["width"] for s in models(view)[0]["segments"]) == pytest.approx(
        1000 * view["total"]["value"] / view["scale"]
    )
    assert all(
        r["provider"] == "anthropic" and r["role"] == "Model turns" for r in view["table_rows"]
    )


def test_reasoning_is_never_added_to_accounted_volume():
    doc = document()
    doc["rows"] = [doc["rows"][3]]
    doc["rows"][0].update(fresh_input=10, cache_write=20, cache_read=30, output=40, reasoning=35)
    view = projected(doc=doc)
    assert view["total"]["value"] == 100
    assert [s["key"] for s in models(view)[0]["segments"]] == list(us.ADDITIVE_CLASSES)
    assert projected({"measure": "reasoning"}, doc)["total"]["value"] == 35
    assert projected({"measure": "output"}, doc)["total"]["value"] == 40


def test_unknown_partial_zero_and_no_matching_records_are_distinct():
    unknown = projected({"provider": "zai", "role": "compaction"})
    assert unknown["total"]["value"] is None
    assert "Unknown volume" in rendered(unknown)
    assert "Measured zero" not in rendered(unknown)
    partial = projected({"provider": "zai", "role": "turn"})
    assert partial["total"]["unknown"] == 1
    assert partial["total"]["text"].startswith("≥ ")
    doc = document()
    doc["rows"] = [doc["rows"][0]]
    doc["rows"][0].update({k: 0 for k in ("requests", *us.TOKEN_CLASSES)})
    zero = projected(doc=doc)
    assert zero["total"]["text"] == "0" and zero["total"]["unknown"] == 0
    assert "Measured zero" in rendered(zero)
    empty = projected({"provider": "anthropic", "model": "glm-5.3"})
    assert empty["matched_rows"] == 0
    assert "No rows match this scope" in rendered(empty)
    assert "Source gap" not in rendered(empty)


def test_partial_known_zero_is_not_a_measured_zero():
    doc = document()
    doc["rows"] = [doc["rows"][0]]
    doc["rows"][0].update(fresh_input=0, cache_write=None, cache_read=0, output=0)
    view = projected(doc=doc)
    assert view["total"]["text"] == "≥ 0"
    assert "No known volume" in rendered(view)
    assert "Measured zero" not in rendered(view)


@pytest.mark.parametrize("measure", [key for key, _ in ob.MEASURES])
def test_absolute_scale_and_stable_order_survive_scope_filters(measure):
    whole = projected({"measure": measure})
    subset = projected({"measure": measure, "provider": "anthropic", "role": "turn"})
    assert subset["scale"] == whole["scale"]
    assert [p["provider"] for p in whole["providers"]] == sorted(
        p["provider"] for p in whole["providers"]
    )
    for model in models(whole):
        assert sum(s["width"] for s in model["segments"]) <= 1000.000001
    assert whole["unit"] == ("recorded calls" if measure == "requests" else "tokens")


@pytest.mark.parametrize(
    "count, expected",
    [(9, ["0", "2.5", "5", "7.5", "10"]), (49, ["0", "12.5", "25", "37.5", "50"])],
)
def test_small_count_axis_labels_exact_quarters(count, expected):
    doc = document()
    doc["rows"] = [doc["rows"][0]]
    doc["rows"][0]["requests"] = count
    view = projected({"measure": "requests"}, doc)
    assert [tick["text"] for tick in view["ticks"]] == expected


def test_single_known_class_has_own_unit_and_no_implied_quota():
    view = projected({"measure": "requests", "role": "compaction"})
    assert view["total"]["text"] == "≥ 3"
    assert view["unit"] == "recorded calls"
    assert view["legend"] == [("requests", "Recorded calls")]
    html = rendered(view)
    assert "quota denominator" in html and "physical API attempts" in html
    assert "requests per second" not in html and "remaining capacity" not in html


@pytest.mark.parametrize("age,level", [(60, "ok"), (3600, "warn"), (86400, "crit")])
def test_freshness_uses_earlier_source_time(age, level):
    doc = document()
    doc["generated_at"] = doc["coverage"]["window_end"]
    source = us.parse(doc)
    view = projected(doc=doc, now=source.window_end.timestamp() + age)
    assert view["freshness"] == level
    assert view["observed_iso"] == source.window_end.isoformat()


def test_reexporting_frozen_source_does_not_make_it_fresh():
    doc = document()
    doc["generated_at"] = "2026-11-11T00:00:00Z"
    clock = us.parse(doc).generated_at.timestamp() + 30
    assert projected(doc=doc, now=clock)["freshness"] == "crit"


def test_future_publication_or_coverage_is_invalid():
    source = summary()
    assert (
        projected(now=source.generated_at.timestamp() - 1800)["freshness"] == "invalid-source-time"
    )
    doc = document()
    doc["coverage"]["window_end"] = "2026-11-09T00:00:00Z"
    assert projected(doc=doc)["freshness"] == "invalid-source-time"


def test_source_coverage_stays_whole_window_through_filters():
    whole = projected()
    filtered = projected({"provider": "anthropic", "role": "compaction"})
    for key in ("window_start", "window_end", "window_span", "generated_at", "source_rows"):
        assert whole[key] == filtered[key]
    assert "smaller time buckets" in rendered(filtered)
    assert "verified workspace ownership" in rendered(filtered)


def test_template_escapes_labels_and_error_text():
    view = copy.deepcopy(projected())
    payload = '<img src=x onerror="alert(1)">'
    view["providers"][0]["provider"] = payload
    view["providers"][0]["models"][0]["model"] = payload
    view["table_rows"][0]["pane"] = payload
    html = rendered(view)
    assert payload not in html and "&lt;img" in html
    html = rendered({"state": "error", "error": payload})
    assert payload not in html and "&lt;img" in html


def test_template_has_no_script_or_automatic_refresh():
    html = rendered(projected())
    assert "<script" not in html and "hx-trigger" not in html
    assert 'method="get"' in html and 'method="post"' not in html
    assert "<table" in html and "<caption" in html
    assert 'id="ob-exact-data"' in html and 'tabindex="0"' in html


@pytest.mark.parametrize(
    "query",
    [{"provider": "missing"}, {"model": "<script>"}, {"role": "retry"}, {"measure": "quota"}],
)
def test_unsupported_filters_are_rejected_without_silent_fallback(query):
    with pytest.raises(ValueError, match="Unknown"):
        projected(query)


@pytest.fixture(autouse=True)
def no_live_sources(monkeypatch):
    monkeypatch.setattr(usage, "_cache", {"key": None, "result": (None, None)})
    monkeypatch.delenv(usage.ENV, raising=False)
    monkeypatch.delenv(auth.ENV, raising=False)


async def client():
    app = web.Application()
    routes.attach_ui(app)
    app.on_startup.clear()
    app.on_cleanup.clear()
    result = TestClient(TestServer(app))
    await result.start_server()
    return result


async def test_route_reuses_loader_is_get_only_and_marks_missing(monkeypatch, tmp_path):
    c = await client()
    try:
        response = await c.get("/ui/observatory")
        assert response.status == 200
        html = await response.text()
        assert "No published usage summary" in html and "Usage observation unavailable" in html
        assert response.headers["Cache-Control"] == "no-store"
        assert (await c.post("/ui/observatory")).status == 405
        p = tmp_path / "usage.json"
        p.write_text(FIXTURE.read_text())
        monkeypatch.setenv(usage.ENV, str(p))
        response = await c.get("/ui/observatory?provider=anthropic&role=turn")
        html = await response.text()
        assert response.status == 200 and "claude-opus-5-5" in html
        assert 'value="anthropic" selected' in html and 'value="turn" selected' in html
        assert usage._cache["key"][0] == str(p)
        assert (await c.get("/ui/observatory?measure=quota")).status == 400
    finally:
        await c.close()


async def test_invalid_summary_is_explicit_and_never_echoes_source(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text('{"prompt":"PRIVATE-BAD-SOURCE"}')
    monkeypatch.setenv(usage.ENV, str(p))
    c = await client()
    try:
        html = await (await c.get("/ui/observatory")).text()
        assert "Usage observation unavailable" in html
        assert "PRIVATE-BAD-SOURCE" not in html
    finally:
        await c.close()


async def test_auth_guard_blocks_remote_before_read_and_allows_existing_gate(monkeypatch):
    gate = auth.Gate(True, b"synthetic-observatory-token")
    monkeypatch.setattr(auth, "current_gate", lambda: gate)
    reads = []
    monkeypatch.setattr(
        ob, "view", lambda query: reads.append(query) or {"state": "missing", "error": "fixture"}
    )
    c = await client()
    try:
        remote = {"X-Forwarded-For": "203.0.113.7"}
        denied = await c.get("/ui/observatory", headers=remote, allow_redirects=False)
        assert denied.status == 303 and not reads
        denied = await c.get("/ui/observatory", headers={**remote, "HX-Request": "true"})
        assert denied.status == 401 and not reads
        allowed = await c.get(
            "/ui/observatory", headers={**remote, auth.AUTH_HEADER: "synthetic-observatory-token"}
        )
        assert allowed.status == 200 and len(reads) == 1
        assert "synthetic-observatory-token" not in await allowed.text()
    finally:
        await c.close()
