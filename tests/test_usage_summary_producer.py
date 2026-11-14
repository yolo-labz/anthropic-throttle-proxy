"""usage-summary.v1 producer (scripts/token-usage-lake/export) on a synthetic receipt."""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import os
import stat
import time
from collections import Counter
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_EXPORT = _ROOT / "scripts/token-usage-lake/export"
_spec = importlib.util.spec_from_file_location("usp", _EXPORT / "usage_summary_producer.py")
usp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usp)

# 08/11/2026 21:00 BRT: the "scan" ends before the BRT day does.
SCANNED_AT = dt.datetime(2026, 11, 9, 0, 0, tzinfo=dt.UTC).timestamp()
NOW = dt.datetime(2026, 11, 9, 0, 5, tzinfo=dt.UTC)
IN_WINDOW = "2026-11-08T12:00:00.000Z"
BEFORE = "2026-11-06T12:00:00.000Z"


def _line(**entry) -> str:
    return json.dumps(entry) + "\n"


def _turn(model: str, ts: str, provider: str = "claude-lane") -> str:
    msg = {
        "role": "assistant",
        "provider": provider,
        "model": model,
        "content": [{"type": "text", "text": "SECRET-BODY"}],
        "usage": {"input": 1},
    }
    return _line(type="message", id="x", timestamp=ts, message=msg)


@pytest.fixture
def receipt(tmp_path) -> dict:
    doc = json.loads((_EXPORT / "fixtures/receipt.synthetic.json").read_text(encoding="utf-8"))
    (tmp_path / "a.jsonl").write_text(
        _line(type="session", id="s", timestamp=BEFORE, cwd="/x")
        + _turn("claude-haiku-5-5", BEFORE)  # out of window: must not count as a 2nd model
        + _line(
            type="model_change",
            id="m",
            timestamp=IN_WINDOW,
            provider="claude-lane",
            modelId="claude-opus-5-5",
        )
        + _turn("claude-opus-5-5", IN_WINDOW)
        + _line(type="compaction", id="c", timestamp=IN_WINDOW, summary="SECRET-SUMMARY")
        + "{not json\n",
        encoding="utf-8",
    )
    (tmp_path / "b.jsonl").write_text(
        _turn("claude-opus-5-5", IN_WINDOW) + _turn("claude-haiku-5-5", IN_WINDOW),
        encoding="utf-8",
    )
    for top in doc["top_files_by_cache_write"]:
        top["source"] = str(tmp_path / Path(top["source"]).name)
    return doc


def _rows(doc: dict) -> dict:
    return {(r["role"], r["provider"], r["model"], r["pane"]): r for r in doc["rows"]}


def test_totals_equal_the_receipt_per_class(receipt) -> None:
    doc = usp.build(receipt, SCANNED_AT, NOW)
    sums: dict[tuple, Counter] = {}
    for r in doc["rows"]:
        c = sums.setdefault((r["role"], r["provider"], r["model"]), Counter())
        c["calls"] += r["requests"]
        for cls in ("fresh_input", "cache_write", "cache_read", "output"):
            c[cls] += r[cls] or 0
    opus = sums[("turn", "claude-lane", "claude-opus-5-5")]
    assert opus == Counter(
        calls=10, fresh_input=100, output=1000, cache_read=50000, cache_write=5000
    )
    comp = sums[("compaction", "claude-lane", "claude-opus-5-5")]
    assert comp["calls"] == 3 and comp["fresh_input"] == 3000 and comp["output"] == 800


def test_attribution_only_for_one_binding_and_one_model(receipt) -> None:
    rows = _rows(usp.build(receipt, SCANNED_AT, NOW))
    a = rows[("turn", "claude-lane", "claude-opus-5-5", "products/wD:pA")]
    assert (a["requests"], a["cache_write"], a["cache_read"]) == (6, 3000, 30000)
    rest = rows[("turn", "claude-lane", "claude-opus-5-5", None)]
    assert (rest["requests"], rest["cache_write"]) == (4, 2000)
    assert ("compaction", "claude-lane", "claude-opus-5-5", "products/wD:pA") in rows
    panes = {r["pane"] for r in rows.values()}
    # b = two models in window, pC = ambiguous binding, pD = unreadable file
    assert panes == {"products/wD:pA", None}


def test_unknown_stays_null_never_zero(receipt) -> None:
    rows = _rows(usp.build(receipt, SCANNED_AT, NOW))
    assert all(r["reasoning"] is None for r in rows.values())
    gpt = rows[("turn", "openai-codex", "gpt-6.1-sol", None)]
    assert gpt["cache_write"] is None and gpt["cache_read"] == 2000  # not reported != 0
    haiku = rows[("turn", "claude-lane", "claude-haiku-5-5", None)]
    assert haiku["cache_read"] is None and haiku["cache_write"] == 900  # 1 uncountable row


def test_coverage_window_and_generated_at(receipt) -> None:
    doc = usp.build(receipt, SCANNED_AT, NOW)
    assert doc["coverage"] == {
        "window_start": "2026-11-08T03:00:00Z",
        "window_end": "2026-11-09T00:00:00Z",
        "files_scanned": 4,
        "files_skipped": 0,
    }
    assert doc["generated_at"] == "2026-11-09T00:00:00Z"  # source as-of, not NOW
    late = usp.build(receipt, SCANNED_AT + 86400, NOW + dt.timedelta(days=1))
    assert late["coverage"]["window_end"] == "2026-11-09T03:00:00Z"  # capped at the day


def test_refuses_to_guess_a_window(receipt) -> None:
    receipt["scope"] = "no day here"
    with pytest.raises(ValueError, match="refusing to guess"):
        usp.build(receipt, SCANNED_AT, NOW)


@pytest.mark.parametrize("field", ["assistant_cacheWrite", "assistant_calls"])
def test_attribution_exceeding_totals_is_rejected(receipt, field) -> None:
    receipt["top_files_by_cache_write"][0][field] = 999999
    report: dict = {}
    rows = _rows(usp.build(receipt, SCANNED_AT, NOW, report))
    assert report["rejected_attributions"] == 1
    assert ("turn", "claude-lane", "claude-opus-5-5", "products/wD:pA") not in rows
    assert rows[("turn", "claude-lane", "claude-opus-5-5", None)]["cache_write"] == 5000


def test_output_is_content_free_and_private(receipt, tmp_path) -> None:
    out = tmp_path / "lake/usage-summary.json"
    usp.write_private(out, usp.build(receipt, SCANNED_AT, NOW))
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700
    blob = out.read_text(encoding="utf-8")
    for leak in ("SECRET", str(tmp_path), "jsonl", "two-models"):
        assert leak not in blob


def test_passes_the_spec_380_validator(receipt, tmp_path) -> None:
    us = pytest.importorskip("anthropic_throttle_proxy.usage_summary")
    out = tmp_path / "u.json"
    usp.write_private(out, usp.build(receipt, SCANNED_AT, NOW))
    s = us.load(out)
    assert s.rejected_rows == 0 and len(s.rows) == len(json.loads(out.read_text())["rows"])


async def _render(doc: dict, tmp_path: Path, monkeypatch) -> str:
    usage = pytest.importorskip("anthropic_throttle_proxy.ui.usage")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from anthropic_throttle_proxy.ui import routes

    out = tmp_path / "u.json"
    usp.write_private(out, doc)
    monkeypatch.setenv(usage.ENV, str(out))
    monkeypatch.setattr(usage, "_cache", {"key": None, "result": (None, None)})
    app = web.Application()
    routes.attach_ui(app)
    usage.attach_usage(app)
    app.on_startup.clear()
    app.on_cleanup.clear()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/ui/usage")
        html = await resp.text()
    finally:
        await client.close()
    assert resp.status == 200 and "usage summary unavailable" not in html
    return html


def _today(receipt: dict) -> float:
    """Re-scope the receipt to today's BRT day; return a just-scanned as-of."""
    now = dt.datetime.now(usp.BRT)
    receipt["scope"] = f"Synthetic, {now:%d/%m/%Y} BRT through scan"
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return max(day + 1, now.timestamp() - 5)


async def test_fresh_source_renders_fresh(receipt, tmp_path, monkeypatch) -> None:
    scanned = _today(receipt)
    html = await _render(
        usp.build(receipt, scanned, dt.datetime.now(dt.UTC)), tmp_path, monkeypatch
    )
    for label in ("fresh input", "cache write", "cache read", "output", "reasoning"):
        assert f"{label}</th>" in html
    assert "unknown" in html and "usage-stale ok" in html


async def test_old_receipt_rerendered_now_stays_stale(receipt, tmp_path, monkeypatch) -> None:
    """Negative control: a timer re-running on an old receipt must not look fresh."""
    receipt["scope"] = "Synthetic, 01/11/2026 BRT through scan"  # a week-old receipt
    old = dt.datetime(2026, 11, 2, 1, 0, tzinfo=dt.UTC).timestamp()  # 01/11 22:00 BRT
    a = usp.build(receipt, old, dt.datetime.now(dt.UTC))
    b = usp.build(receipt, old, dt.datetime.now(dt.UTC) + dt.timedelta(days=3))
    assert a == b and a["generated_at"] == a["coverage"]["window_end"] == "2026-11-02T01:00:00Z"
    html = await _render(a, tmp_path, monkeypatch)
    assert "usage-stale crit" in html
    assert "usage-stale ok" not in html


def test_future_or_invalid_source_is_never_fresh(receipt, tmp_path) -> None:
    scanned = _today(receipt)
    with pytest.raises(ValueError, match="in the future"):
        usp.build(receipt, scanned + 3600, dt.datetime.now(dt.UTC))
    with pytest.raises(ValueError, match="before its own window"):
        usp.build(receipt, scanned - 3 * 86400, dt.datetime.now(dt.UTC))
    src = tmp_path / "receipt.json"
    src.write_text(json.dumps(receipt), encoding="utf-8")
    future = time.time() + 3600
    os.utime(src, (future, future))
    out = tmp_path / "o/usage-summary.json"
    with pytest.raises(ValueError, match="in the future"):
        usp.main(["--receipt", str(src), "--out", str(out)])
    assert not out.exists()  # nothing fresh-looking was written


def test_reasoning_is_a_null_subset_never_added(receipt) -> None:
    assert usp.SUBSET_OF_OUTPUT not in usp.ADDITIVE
    rows = usp.build(receipt, SCANNED_AT, NOW)["rows"]
    assert all(r["reasoning"] is None for r in rows)
    out = sum(r["output"] for r in rows if r["role"] == "turn" and r["model"] == "claude-opus-5-5")
    assert out == receipt["totals"]["assistant/claude-lane/claude-opus-5-5"]["output"]


def test_cli_writes_once(receipt, tmp_path, capsys) -> None:
    src = tmp_path / "receipt.json"
    src.write_text(json.dumps(copy.deepcopy(receipt)), encoding="utf-8")
    out = tmp_path / "o/usage-summary.json"
    assert usp.main(["--receipt", str(src), "--out", str(out)]) == 0
    assert "pane-attributed" in capsys.readouterr().out
    assert json.loads(out.read_text())["schema"] == "usage-summary.v1"
    assert time.time() - out.stat().st_mtime < 60
