"""Spec 380 — usage-summary.v1 contract + the read-only Usage panel."""

from __future__ import annotations

import copy
import inspect
import json
import time
from pathlib import Path

import jinja2
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import proxy
from anthropic_throttle_proxy import usage_summary as us
from anthropic_throttle_proxy.ui import routes, usage

_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = _ROOT / "specs/380-usage-panel/usage-summary.v1.example.json"
_TEMPLATES = _ROOT / "src/anthropic_throttle_proxy/ui/templates"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _write(tmp_path: Path, doc: object) -> Path:
    p = tmp_path / "usage.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(usage, "_cache", {"key": None, "result": (None, None)})
    monkeypatch.delenv(usage.ENV, raising=False)


# --- contract -------------------------------------------------------------


def test_fixture_parses_with_every_class_and_unknowns_kept() -> None:
    s = us.parse(_doc())
    assert s.files_scanned == 42 and s.files_skipped == 1 and s.rejected_rows == 0
    assert {r.role for r in s.rows} == {"turn", "compaction"}
    zai = next(r for r in s.rows if r.provider == "zai" and r.role == "turn")
    assert zai.pane is None and zai.counts["cache_write"] is None  # unknown, not 0
    assert set(zai.counts) == set(us.TOKEN_CLASSES)


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda d: d.update(schema="usage-summary.v2"), "schema"),
        (lambda d: d.update(extra=1), "top-level"),
        (lambda d: d["coverage"].update(note="x"), "coverage keys"),
        (lambda d: d.update(generated_at="2026-11-08T23:40:00"), "offset"),
        (lambda d: d.update(generated_at="yesterday"), "ISO-8601"),
        (lambda d: d["coverage"].update(window_end="2026-11-01T00:00:00Z"), "ends before"),
        (lambda d: d["coverage"].update(files_scanned=-1), "files_scanned"),
        (lambda d: d.update(rows={}), "rows is not a list"),
        (lambda d: d.update(rows=[d["rows"][0]] * (us.MAX_ROWS + 1)), "more than"),
    ],
)
def test_document_level_rejections(mutate, reason) -> None:
    doc = _doc()
    mutate(doc)
    with pytest.raises(us.SummaryError, match=reason):
        us.parse(doc)


@pytest.mark.parametrize(
    "patch",
    [
        {"prompt": "hello"},  # unknown key = possible content
        {"pane": "pedro@example.com"},  # raw identity cannot be a label
        {"model": "two words"},
        {"role": "chat"},
        {"output": 1.5},
        {"output": True},
        {"cache_read": -3},
    ],
)
def test_bad_rows_are_dropped_and_counted(patch) -> None:
    doc = _doc()
    doc["rows"][0].update(patch)
    s = us.parse(doc)
    assert s.rejected_rows == 1 and len(s.rows) == len(doc["rows"]) - 1


def test_row_missing_a_class_is_rejected_not_zeroed() -> None:
    doc = _doc()
    del doc["rows"][0]["reasoning"]
    assert us.parse(doc).rejected_rows == 1


def test_load_rejects_nan_oversize_and_garbage(tmp_path) -> None:
    p = tmp_path / "u.json"
    p.write_text('{"x": NaN}', encoding="utf-8")
    with pytest.raises(us.SummaryError, match="non-finite"):
        us.load(p)
    p.write_bytes(b" " * (us.MAX_BYTES + 1))
    with pytest.raises(us.SummaryError, match="larger"):
        us.load(p)
    p.write_bytes(b"\xff{")
    with pytest.raises(us.SummaryError, match="not valid JSON"):
        us.load(p)
    with pytest.raises(us.SummaryError, match="unreadable"):
        us.load(tmp_path / "missing.json")


def test_total_is_unknown_when_no_row_knows_and_lower_bound_otherwise() -> None:
    rows = us.parse(_doc()).rows
    turns = [r for r in rows if r.role == "turn"]
    assert us.total(turns, "cache_write") == (310000 + 150000 + 0, 1)
    allnull = [r for r in rows if r.role == "compaction" and r.provider == "zai"]
    assert us.total(allnull, "output") == (None, 1)


# --- view + template ------------------------------------------------------


def test_view_hidden_when_env_unset() -> None:
    assert usage.view() == {"state": "hidden"}
    assert usage.enabled() is False


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def test_view_staleness_runs_from_the_source_time(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(usage.ENV, str(_write(tmp_path, _doc())))
    s = us.parse(_doc())
    src = s.window_end.timestamp()  # 10 min before generated_at in the fixture
    assert usage.view(now=src + 60)["stale_level"] == "ok"
    assert usage.view(now=src + 2 * 3600)["stale_level"] == "warn"
    assert usage.view(now=src + 2 * 86400)["stale_level"] == "crit"
    v = usage.view(now=src + 60)
    assert v["as_of"] == "08/11/2026 20:30" and v["generated_at"] == "08/11/2026 20:40"


def _freshness(window_end: float, generated_at: float, now: float) -> str:
    doc = _doc()
    doc["coverage"]["window_start"] = _iso(window_end - 3600)
    doc["coverage"]["window_end"] = _iso(window_end)
    doc["generated_at"] = _iso(generated_at)
    return usage.freshness(us.parse(doc), now)[0]


def test_negative_control_frozen_receipt_rewritten_now_stays_stale() -> None:
    now = time.time()
    # producer re-exports a 2-day-old receipt; generated_at = write time = now
    assert _freshness(window_end=now - 2 * 86400, generated_at=now, now=now) == "crit"


def test_negative_control_future_window_end_is_not_fresh() -> None:
    now = time.time()
    level = _freshness(window_end=now + 600, generated_at=now + 900, now=now)
    assert level == "invalid-source-time"


def test_negative_control_window_end_after_write_is_not_fresh() -> None:
    now = time.time()
    level = _freshness(window_end=now - 60, generated_at=now - 600, now=now)
    assert level == "invalid-source-time"
    # inside the 60 s skew allowance it is still judged normally
    assert _freshness(window_end=now - 60, generated_at=now - 90, now=now) == "ok"


def test_negative_control_reasoning_above_output_is_rejected() -> None:
    doc = _doc()
    doc["rows"][3].update(output=100, reasoning=101)
    s = us.parse(doc)
    assert s.rejected_rows == 1
    doc["rows"][3].update(output=None, reasoning=101)  # unknown output: cannot compare
    assert us.parse(doc).rejected_rows == 0


def test_bar_keeps_classes_separate_and_never_adds_reasoning() -> None:
    b = usage.bar(
        {"fresh_input": 10, "cache_write": None, "cache_read": 30, "output": 60, "reasoning": 50}
    )
    assert [s["key"] for s in b["segments"]] == ["fresh_input", "cache_read", "output"]
    assert [s["w"] for s in b["segments"]] == [10.0, 30.0, 60.0]  # reasoning ⊂ output
    assert b["unknown"] == ["cache_write"]
    assert usage.bar({**dict.fromkeys(us.ADDITIVE_CLASSES, 1), "reasoning": None})["unknown"] == []
    assert usage.bar(dict.fromkeys(us.TOKEN_CLASSES))["empty"] is True


async def _client() -> TestClient:
    app = web.Application()
    routes.attach_ui(app)
    usage.attach_usage(app)
    app.on_startup.clear()
    app.on_cleanup.clear()
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _get_usage() -> tuple[int, str]:
    client = await _client()
    try:
        resp = await client.get("/ui/usage")
        return resp.status, await resp.text()
    finally:
        await client.close()


async def test_route_empty_when_unset() -> None:
    assert await _get_usage() == (200, "")


async def test_route_renders_every_class_separately_and_unknown_as_unknown(
    tmp_path, monkeypatch
) -> None:
    doc = _doc()
    doc["generated_at"] = doc["coverage"]["window_end"] = _iso(time.time())
    monkeypatch.setenv(usage.ENV, str(_write(tmp_path, doc)))
    status, html = await _get_usage()
    assert status == 200
    for label in ("fresh input", "cache write", "cache read", "output", "of which reasoning"):
        assert f"{label}</th>" in html
    for key in us.ADDITIVE_CLASSES:
        assert f'class="u-{key}"' in html
    assert 'class="u-reasoning"' not in html  # never a bar segment
    assert "data as of <b>" in html and "written " in html
    assert "unknown" in html and "≥ " in html  # partial total is a lower bound
    assert "compaction summariser" in html and "model turns" in html
    assert "files scanned <b>42</b>" in html and "1 skipped" in html
    assert "$" not in html  # no spend framing
    assert "usage-stale ok" in html
    assert "<script" not in html


async def test_route_is_failure_tolerant(tmp_path, monkeypatch) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(usage.ENV, str(p))
    status, html = await _get_usage()
    assert status == 200 and "usage summary unavailable" in html and "{not json" not in html
    monkeypatch.setenv(usage.ENV, str(tmp_path / "absent.json"))
    status, html = await _get_usage()
    assert status == 200 and "unreadable" in html


async def test_cache_reloads_when_the_file_changes(tmp_path, monkeypatch) -> None:
    doc = _doc()
    p = _write(tmp_path, doc)
    monkeypatch.setenv(usage.ENV, str(p))
    assert usage.view()["files_scanned"] == 42
    doc2 = copy.deepcopy(doc)
    doc2["coverage"]["files_scanned"] = 7
    p.write_text(json.dumps(doc2, indent=2), encoding="utf-8")  # size changes too
    assert usage.view()["files_scanned"] == 7


@pytest.mark.parametrize("enabled", [False, True])
def test_dashboard_section_only_when_enabled(enabled) -> None:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals["usage_panel_enabled"] = lambda: enabled
    src = env.get_template("dashboard.html").render(asset_v="t", central_url="(direct)")
    assert ('id="usage"' in src) is enabled


def test_health_never_reads_the_summary() -> None:
    assert "usage" not in inspect.getsource(proxy.health)
