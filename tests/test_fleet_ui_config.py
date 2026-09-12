"""fleet-ui YAML config: load/validate/degrade + decorate behaviour."""

from __future__ import annotations

import copy

import pytest

from anthropic_throttle_proxy import fleet_ui_config as fuc

CFG = """
subscriptions:
  - id: codex-c
    label: "Codex C"
    emoji: "🅒"
    family: openai
    plan: pro
    lane: codex:c
  - id: anthropic-c
    label: "Anthropic C"
    emoji: "✳️"
    family: anthropic
    bearer: 666a53af
defaults:
  emoji_by_family:
    github: "🐙"
"""


def _write(tmp_path, text=CFG):
    p = tmp_path / "fleet-ui.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _live_rows():
    return [
        {
            "id": "codex:b",
            "family": "openai",
            "icon": "🤖",
            "meters": [{"label": "codex", "pct": 96}],
            "status": "ok",
        },
        {
            "id": "anthropic-c",
            "identity": "phsb5321@gmail.com",
            "family": "anthropic",
            "icon": "✳️",
            "bearer_id": "666a53af",
            "meters": [{"label": "7d", "pct": 0}],
            "status": "refused",
        },
        {
            "id": "codex:c",
            "family": "openai",
            "icon": "🤖",
            "meters": [{"label": "codex", "pct": 0}],
            "status": "unknown",
        },
    ]


def test_load_and_validate(tmp_path):
    cfg = fuc.load(_write(tmp_path))
    assert cfg["config_error"] is None
    assert [s["id"] for s in cfg["subscriptions"]] == ["codex-c", "anthropic-c"]


def test_missing_config_is_honest(tmp_path):
    cfg = fuc.load(tmp_path / "nope.yaml")
    assert cfg["subscriptions"] == []
    assert "config missing" in cfg["config_error"]


def test_bad_edit_degrades_to_last_good(tmp_path):
    p = _write(tmp_path)
    fuc.load(p)
    good_mtime = p.stat().st_mtime + 1
    import os

    os.utime(p, (good_mtime, good_mtime))
    p.write_text("subscriptions: 42", encoding="utf-8")
    os.utime(p, (good_mtime + 1, good_mtime + 1))
    cfg = fuc.load(p)
    assert cfg["config_error"] and "must be a list" in cfg["config_error"]
    # last good config survived
    assert [s["id"] for s in cfg["subscriptions"]] == ["codex-c", "anthropic-c"]
    assert fuc.load(p)["config_error"] == cfg["config_error"]  # every poll reports it


def test_decorate_merges_config_onto_live_rows(tmp_path):
    cfg = fuc.load(_write(tmp_path))
    rows = _live_rows()
    out = fuc.decorate(rows, cfg)["rows"]
    by_id = {r["id"]: r for r in out}
    # The live lane id is PRESERVED (router/test identity); the config
    # decorates presentation: label + emoji.
    assert by_id["codex:c"]["icon"] == "🅒"
    assert by_id["codex:c"]["label"] == "Codex C"
    assert by_id["codex:c"]["configured"] is True
    assert by_id["codex:c"]["meters"][0]["pct"] == 0  # live meters kept
    assert by_id["anthropic-c"]["label"] == "Anthropic C"
    assert by_id["anthropic-c"]["status"] == "refused"  # live status kept
    assert by_id["codex:b"]["icon"] == "🤖"  # unconfigured rows keep their icon
    # config order leads: the two configured rows render first
    assert [r["id"] for r in out][:2] == ["codex:c", "anthropic-c"]


def test_configured_row_without_live_data_renders_no_reading(tmp_path):
    text = CFG.replace("    lane: codex:c\n", "")
    cfg = fuc.load(_write(tmp_path, text))
    out = fuc.decorate([], cfg)["rows"]
    row = next(r for r in out if r["id"] == "codex-c")
    assert row["status"] == "unknown"
    assert row["meters"][0]["note"] == "no reading"


def test_validate_rejects_bad_shapes(tmp_path):
    from anthropic_throttle_proxy.fleet_ui_config import _validate

    with pytest.raises(ValueError):
        _validate([])
    with pytest.raises(ValueError):
        _validate({"subscriptions": [{"id": "x"}]})  # missing label/family


@pytest.mark.parametrize(
    "bad",
    [
        "defaults: []\n",
        "defaults: {emoji_by_family: []}\n",
        "defaults: {emoji_by_family: {openai: [bad]}}\n",
        "subscriptions: [{id: x, label: X, family: openai, lane: [codex:c]}]\n",
        "subscriptions: [{id: x, label: X, family: openai, emoji: 2}]\n",
        "subscriptions: [{id: x, label: X, family: openai}, {id: x, label: Y, family: openai}]\n",
    ],
)
def test_invalid_optional_fields_keep_last_good(tmp_path, bad):
    p = _write(tmp_path)
    good = fuc.load(p)
    prefix = "subscriptions: []\n" if bad.startswith("defaults:") else ""
    p.write_text(prefix + bad, encoding="utf-8")
    invalid = fuc.load(p)
    assert invalid["config_error"]
    assert invalid["subscriptions"] == good["subscriptions"]
    assert fuc.load(p)["config_error"] == invalid["config_error"]
    assert fuc.decorate([], invalid)["rows"]


def test_deleted_config_reports_stale_configuration(tmp_path):
    p = _write(tmp_path)
    good = fuc.load(p)
    p.unlink()
    stale = fuc.load(p)
    assert stale["subscriptions"] == good["subscriptions"]
    assert stale["config_error"]


def test_source_aliases_do_not_duplicate_or_mutate_live_rows(tmp_path):
    rows = _live_rows()
    before = copy.deepcopy(rows)
    text = CFG.replace("lane: codex:c", "lane: lane:codex:c")
    cfg = fuc.load(_write(tmp_path, text))
    cfg["subscriptions"].append(
        {"id": "alias", "label": "Duplicate alias", "family": "openai", "lane": "codex:c"}
    )
    out = fuc.decorate(rows, cfg)["rows"]
    assert rows == before
    assert len(out) == len(rows)
    assert out[0]["id"] == "codex:c"
    assert out[0]["label"] == "Codex C"  # first declaration owns presentation


def test_label_does_not_replace_live_identity(tmp_path):
    rows = _live_rows()
    rows[2]["identity"] = "verified@example.test"
    cfg = fuc.load(_write(tmp_path))
    cfg["subscriptions"][0]["identity"] = "configured@example.test"
    out = fuc.decorate(rows, cfg)["rows"]
    assert out[0]["identity"] == "verified@example.test"


def test_missing_live_row_has_label(tmp_path):
    cfg = fuc.load(_write(tmp_path))
    row = fuc.decorate([], cfg)["rows"][0]
    assert row["label"] == "Codex C"


def test_optional_missing_default_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("FLEET_UI_CONFIG")
    monkeypatch.setattr(fuc, "DEFAULT_PATH", tmp_path / "optional.yaml")
    assert fuc.load()["config_error"] is None


def test_oversize_edit_keeps_last_good(tmp_path):
    p = _write(tmp_path)
    good = fuc.load(p)
    p.write_text("#" + "x" * 32_768, encoding="utf-8")
    cfg = fuc.load(p)
    assert "exceeds" in cfg["config_error"]
    assert cfg["subscriptions"] == good["subscriptions"]
