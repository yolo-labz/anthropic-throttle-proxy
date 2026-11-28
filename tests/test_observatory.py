"""The Observatory's source, scale, uncertainty, route, and safety contract.

Synthetic source: specs/380-usage-panel/usage-summary.v1.example.json.
No live provider calls, account refreshers, timers, or credential reads.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
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


def _lineage_pair() -> tuple[bytes, bytes]:
    a = document()
    b = copy.deepcopy(a)
    b["rows"][0]["requests"] = 121  # Same token headline; different accepted data.
    return json.dumps(a, indent=2).encode() + b"\n", json.dumps(b, indent=2).encode() + b"\n"


async def test_loaded_summary_lineage_tracks_atomic_replacement(tmp_path, monkeypatch):
    path = tmp_path / "private-fixture.json"
    a, b = _lineage_pair()
    digest_a, digest_b = (hashlib.sha256(blob).hexdigest() for blob in (a, b))
    path.write_bytes(a)
    monkeypatch.setenv(usage.ENV, str(path))
    original = path.stat()
    reads = []
    load = us.load

    def counted_load(path):
        reads.append(path)
        return load(path)

    monkeypatch.setattr(us, "load", counted_load)
    summary_a, error = usage._read(path)
    assert error is None and summary_a.rows[0].requests == 120
    assert usage._read(path)[0] is summary_a and reads == [path]
    c = await client()
    try:
        first = await (await c.get("/ui/observatory")).text()
        replacement = tmp_path / "new-summary.json"
        replacement.write_bytes(b)
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        replacement.replace(path)
        updated = path.stat()
        assert (updated.st_mtime_ns, updated.st_size) == (original.st_mtime_ns, original.st_size)
        assert updated.st_ino != original.st_ino
        summary_b, error = usage._read(path)
        assert error is None and summary_b.rows[0].requests == 121
        second = await (await c.get("/ui/observatory")).text()
        assert usage._read(path)[0] is summary_b and reads == [path, path]
        assert summary_a.loaded_sha256 == digest_a and summary_b.loaded_sha256 == digest_b
        assert digest_a != digest_b
        assert f"<code>{digest_a}</code>" in first and digest_b not in first
        assert f"<code>{digest_b}</code>" in second and digest_a not in second
        assert (
            first.split('class="ob-total"')[1].split("</p>")[0]
            == second.split('class="ob-total"')[1].split("</p>")[0]
        )
        assert str(path) not in first + second
    finally:
        await c.close()


@pytest.mark.parametrize("failure", ["bad-json", "bad-schema", "oversize", "unreadable", "missing"])
async def test_loaded_summary_lineage_is_absent_on_failure(tmp_path, monkeypatch, failure):
    path = tmp_path / "private-fixture.json"
    a, _ = _lineage_pair()
    path.write_bytes(a)
    digest = hashlib.sha256(a).hexdigest()
    monkeypatch.setenv(usage.ENV, str(path))
    assert usage._read(path)[0] is not None
    marker = b"synthetic-private-data"
    if failure == "bad-json":
        path.write_bytes(marker)
    elif failure == "bad-schema":
        path.write_bytes(json.dumps({"schema": marker.decode()}).encode())
    elif failure == "oversize":
        path.write_bytes(b" " * (us.MAX_BYTES + 1))
    elif failure == "unreadable":
        path.unlink()
        path.mkdir()  # Stat succeeds but a directory cannot be loaded.
    else:
        path.unlink()
    c = await client()
    try:
        response = await c.get("/ui/observatory")
        html = await response.text()
        assert response.status == 200 and "Usage observation unavailable" in html
        assert "Loaded summary SHA-256" not in html and digest not in html
        assert marker.decode() not in html and str(path) not in html
        assert usage._read(path)[0] is None
    finally:
        await c.close()


def test_loaded_summary_lineage_hashes_only_bytes_that_were_validated(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    a, b = _lineage_pair()
    path.write_bytes(a)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b)
    parse = us.parse

    def rotate_during_validation(doc):
        parsed = parse(doc)
        assert parsed.loaded_sha256 is None
        replacement.replace(path)
        return parsed

    monkeypatch.setattr(us, "parse", rotate_during_validation)
    cached, error = usage._read(path)
    assert error is None and cached.rows[0].requests == 120
    assert cached.loaded_sha256 == hashlib.sha256(a).hexdigest()
    monkeypatch.setattr(us, "parse", parse)
    cached, error = usage._read(path)
    assert error is None and cached.rows[0].requests == 121
    assert cached.loaded_sha256 == hashlib.sha256(b).hexdigest()
    assert "Loaded summary SHA-256" not in rendered(projected())


@pytest.fixture
async def scoped_client(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    path.write_text(FIXTURE.read_text())
    monkeypatch.setenv(usage.ENV, str(path))
    c = await client()
    try:
        yield c
    finally:
        await c.close()


_SCOPE_SELECTION = {
    "provider": "anthropic",
    "model": "claude-opus-5-5",
    "role": "turn",
    "measure": "requests",
}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("account", "synthetic-private-account"),
        ("from", "synthetic-private-start"),
        ("to", "synthetic-private-end"),
        ("pane", ""),
        ("resolution", "synthetic-private-resolution"),
        ("mode", "live"),
        ("mode", "frozen"),
        ("as_of", "synthetic-private-observation"),
    ],
)
async def test_query_scope_rejects_unsupported_keys(scoped_client, key, value):
    response = await scoped_client.get("/ui/observatory", params={**_SCOPE_SELECTION, key: value})
    body = await response.text()
    assert response.status == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert f"Unsupported Observatory selection: {key}." in body
    assert "ob-total" not in body and "synthetic-private" not in body
    assert usage._cache["key"] is None


@pytest.mark.parametrize(
    ("key", "other"),
    [("provider", "zai"), ("model", "glm-5.3"), ("role", ""), ("measure", "tokens")],
)
async def test_query_scope_rejects_conflicts_in_both_orders(scoped_client, key, other):
    siblings = [(k, v) for k, v in _SCOPE_SELECTION.items() if k != key]
    values = (_SCOPE_SELECTION[key], other)
    expected = f"Conflicting {key} filter values."
    for pair in (values, values[::-1]):
        response = await scoped_client.get(
            "/ui/observatory", params=[*siblings, (key, pair[0]), (key, pair[1])]
        )
        assert response.status == 400
        assert response.headers["Cache-Control"] == "no-store"
        assert (await response.text()).startswith(expected)
    assert usage._cache["key"] is None


async def test_query_scope_roundtrip_and_clear(scoped_client):
    selected = list(_SCOPE_SELECTION.items())
    measure = ("measure", "requests")
    response = await scoped_client.get(
        "/ui/observatory", params=[*selected, measure, ("_", "synthetic-private-cache-bust")]
    )
    assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
    html = await response.text()
    for value in _SCOPE_SELECTION.values():
        assert f'value="{value}" selected' in html
    assert "2 of 6 accepted source rows" in html and "synthetic-private" not in html
    assert 'href="/ui/observatory">Clear filters' in html

    # An explicit empty provider leaves all the other selections intact.
    response = await scoped_client.get(
        "/ui/observatory", params={**_SCOPE_SELECTION, "provider": ""}
    )
    assert response.status == 200
    html = await response.text()
    for key in ("model", "role", "measure"):
        assert f'value="{_SCOPE_SELECTION[key]}" selected' in html
    assert 'value="anthropic" selected' not in html

    response = await scoped_client.get("/ui/observatory")
    assert response.status == 200
    html = await response.text()
    assert 'value="tokens" selected' in html and "6 of 6 accepted source rows" in html


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
