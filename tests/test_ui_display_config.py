"""Schema regressions for the fleet-ui config's OPTIONAL presentation keys.

``presentation.apply_display`` (layout worker) consumes
``defaults.hidden_families`` (list[str] — families hidden from the active
board) and ``defaults.show_primary`` (bool). This file owns the VALIDATION
contract only:

* well-typed keys are accepted and pass through unchanged;
* wrong types are rejected with ValueError (``load()`` degrades to
  ``config_error``, never a crash);
* keys ABSENT from a config change nothing — configs written before the keys
  existed keep loading (backwards compatible);
* no hiding behavior and no global default hide live here — ``decorate`` and
  ``apply_display`` own rendering (one end-to-end regression pins that a
  LOADED config actually drives the projection — review B1 found load()
  dropping the keys between the two layers);
"""

import pytest

from anthropic_throttle_proxy import fleet_ui_config
from anthropic_throttle_proxy.ui.presentation import apply_display


def _raw(**defaults):
    return {"subscriptions": [], "defaults": defaults}


def _write_config(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- _validate: acceptance ----------------------------------------------------


def test_validate_accepts_optional_presentation_keys():
    raw = _raw(hidden_families=["codex", "copilot"], show_primary=False)
    validated = fleet_ui_config._validate(raw)
    assert validated is raw  # pass-through: no projection, no mutation
    assert validated["defaults"]["hidden_families"] == ["codex", "copilot"]
    assert validated["defaults"]["show_primary"] is False


def test_validate_accepts_empty_hidden_families_list():
    validated = fleet_ui_config._validate(_raw(hidden_families=[]))
    assert validated["defaults"]["hidden_families"] == []


# --- _validate: wrong types are rejected --------------------------------------


@pytest.mark.parametrize("bad", ["codex", 7, {"codex": True}, [["codex"]], None])
def test_validate_rejects_non_list_or_non_string_hidden_families(bad):
    with pytest.raises(ValueError, match="hidden_families"):
        fleet_ui_config._validate(_raw(hidden_families=bad))


@pytest.mark.parametrize("bad", ["true", 1, 0, None, [], {"show": True}])
def test_validate_rejects_non_boolean_show_primary(bad):
    with pytest.raises(ValueError, match="show_primary"):
        fleet_ui_config._validate(_raw(show_primary=bad))


def test_validate_absent_presentation_keys_is_backwards_compatible():
    # A pre-existing config without the keys validates unchanged.
    raw = {
        "subscriptions": [{"id": "codex:a", "label": "Codex A", "family": "openai"}],
        "defaults": {"emoji_by_family": {"openai": "🧠"}},
    }
    assert fleet_ui_config._validate(raw) is raw


def test_validate_defaults_without_the_keys_still_validates():
    raw = {"subscriptions": [], "defaults": {}}
    assert fleet_ui_config._validate(raw) == raw


# --- load(): synthetic config round-trip --------------------------------------


def test_load_accepts_synthetic_config_with_presentation_keys(tmp_path):
    path = _write_config(
        tmp_path,
        "with-presentation.yaml",
        "subscriptions:\n"
        "  - id: codex:a\n"
        "    label: Codex A\n"
        "    family: openai\n"
        "defaults:\n"
        "  hidden_families:\n"
        "    - github\n"
        "  show_primary: false\n"
        "  emoji_by_family:\n"
        "    openai: 🧠\n",
    )
    cfg = fleet_ui_config.load(path)
    assert cfg["config_error"] is None
    assert [s["id"] for s in cfg["subscriptions"]] == ["codex:a"]
    assert cfg["defaults"]["emoji_by_family"]["openai"] == "🧠"
    # Review B1: load() must PRESERVE the presentation keys it validates —
    # dropping them here silently disabled hiding and show_primary for every
    # loaded config while the old test only looked at subscriptions+emoji.
    assert cfg["defaults"]["hidden_families"] == ["github"]
    assert cfg["defaults"]["show_primary"] is False


def test_loaded_display_config_actually_drives_the_display_projection(tmp_path):
    """Review B1 end-to-end: load → projection, not load → dropped keys."""
    path = _write_config(
        tmp_path,
        "hiding.yaml",
        "subscriptions: []\n"
        "defaults:\n"
        "  hidden_families:\n"
        "    - anthropic\n"
        "  show_primary: false\n",
    )
    cfg = fleet_ui_config.load(path)
    assert cfg["config_error"] is None
    view = {
        "subscriptions": [
            {"id": "claude-a", "family": "anthropic"},
            {"id": "codex-a", "family": "openai"},
        ],
        "providers": [{"kind": "local"}, {"kind": "sibling"}],
        "bearers": [{"bearer_id": "x"}],
        "signals": {"k": "v"},
        "identity": {"a": "b"},
        "status": {"level": "healthy", "verdict": "HEALTHY"},
    }
    out = apply_display(view, cfg)
    assert [row["id"] for row in out["subscriptions"]] == ["codex-a"]
    assert out["show_local"] is False
    assert out["providers"] == [{"kind": "sibling"}]


def test_load_rejects_wrong_hidden_families_type_with_config_error(tmp_path):
    path = _write_config(
        tmp_path,
        "bad-hidden.yaml",
        "subscriptions: []\ndefaults:\n  hidden_families: codex\n",
    )
    cfg = fleet_ui_config.load(path)
    assert cfg["config_error"] is not None
    assert "hidden_families" in cfg["config_error"]
    assert cfg["subscriptions"] == []


def test_load_rejects_non_boolean_show_primary_with_config_error(tmp_path):
    path = _write_config(
        tmp_path,
        "bad-show-primary.yaml",
        "subscriptions: []\ndefaults:\n  show_primary: 1\n",
    )
    cfg = fleet_ui_config.load(path)
    assert cfg["config_error"] is not None
    assert "show_primary" in cfg["config_error"]


def test_load_without_presentation_keys_is_unchanged(tmp_path):
    path = _write_config(tmp_path, "plain.yaml", "subscriptions: []\n")
    cfg = fleet_ui_config.load(path)
    assert cfg["config_error"] is None
    assert cfg["subscriptions"] == []


def test_load_missing_file_stays_safe(tmp_path):
    cfg = fleet_ui_config.load(tmp_path / "absent.yaml")
    assert cfg["subscriptions"] == []
    assert "missing or unreadable" in (cfg["config_error"] or "")
