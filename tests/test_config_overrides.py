"""Unit tests for the runtime-override mechanism added in PR #22.

Covers:
    * Load → empty file = no-op, leaves env defaults in place.
    * set_override coerces + validates + persists + propagates to module attr.
    * reset_override removes the entry, restores env default, persists.
    * Out-of-range and unknown-key inputs raise with a helpful message.
    * Round-trip: load → set → save → restart-simulate (re-import) → load.
"""

from __future__ import annotations

import json

import pytest

from anthropic_throttle_proxy import body_shrink, config


@pytest.fixture(autouse=True)
def isolate_overrides(monkeypatch, tmp_path):
    """Run every test against a private XDG_STATE_HOME so the on-host file is untouched."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        config,
        "OVERRIDES_FILE",
        tmp_path / "anthropic-throttle-proxy" / "overrides.json",
    )
    config.RUNTIME_OVERRIDES.clear()
    config.ENV_DEFAULTS.clear()
    config._capture_env_defaults()
    yield
    config.RUNTIME_OVERRIDES.clear()


def test_capture_env_defaults_populates_every_knob():
    assert set(config.ENV_DEFAULTS.keys()) == set(config.EDITABLE_KNOBS.keys())
    # The non-None spec'd knobs we know up front:
    assert isinstance(config.ENV_DEFAULTS["max_concurrent"], int)
    assert isinstance(config.ENV_DEFAULTS["min_dispatch_gap_ms"], int)
    assert isinstance(config.ENV_DEFAULTS["advisor_enabled"], bool)


def test_load_overrides_with_no_file_is_noop():
    # Sanity: file doesn't exist yet.
    assert not config.OVERRIDES_FILE.is_file()
    config.load_overrides()
    assert config.RUNTIME_OVERRIDES == {}


def test_set_override_coerces_int_and_propagates(monkeypatch):
    config.set_override("max_concurrent", "16")
    assert config.RUNTIME_OVERRIDES["max_concurrent"] == 16
    assert config.MAX_CONCURRENT == 16
    saved = json.loads(config.OVERRIDES_FILE.read_text())
    assert saved["max_concurrent"] == 16


def test_set_override_min_dispatch_gap_ms_converts_to_seconds():
    config.set_override("min_dispatch_gap_ms", "250")
    assert config.MIN_DISPATCH_GAP_S == pytest.approx(0.25)


def test_set_override_central_local_max_concurrent_propagates():
    config.set_override("central_local_max_concurrent", "6")
    assert config.RUNTIME_OVERRIDES["central_local_max_concurrent"] == 6
    assert config.CENTRAL_LOCAL_MAX_CONCURRENT == 6


def test_set_override_aimd_initial_concurrent_propagates():
    try:
        config.set_override("aimd_initial_concurrent", "3")
        assert config.RUNTIME_OVERRIDES["aimd_initial_concurrent"] == 3
        assert config.AIMD_INITIAL_CONCURRENT == 3
    finally:
        config.reset_override("aimd_initial_concurrent")


def test_set_override_bounds_check_rejects_too_low():
    with pytest.raises(ValueError, match="below min"):
        config.set_override("max_concurrent", "0")


def test_set_override_bounds_check_rejects_too_high():
    with pytest.raises(ValueError, match="above max"):
        config.set_override("max_concurrent", "999999")


def test_set_override_unknown_key_raises():
    with pytest.raises(KeyError):
        config.set_override("not_a_real_knob", "1")


def test_set_override_bool_accepts_truthy_strings():
    config.set_override("advisor_enabled", "true")
    assert config.RUNTIME_OVERRIDES["advisor_enabled"] is True
    config.set_override("advisor_enabled", "no")
    assert config.RUNTIME_OVERRIDES["advisor_enabled"] is False


def test_set_override_body_shrink_propagates_to_other_module():
    """body_shrink.CAP_BYTES lives in a different module — verify setattr crosses the boundary."""
    config.set_override("body_shrink_cap_bytes", "10485760")  # 10 MiB
    assert body_shrink.CAP_BYTES == 10485760


def test_reset_override_restores_env_default_and_removes_entry(monkeypatch):
    env_default_max = config.ENV_DEFAULTS["max_concurrent"]
    config.set_override("max_concurrent", "8")
    assert config.MAX_CONCURRENT == 8
    config.reset_override("max_concurrent")
    assert "max_concurrent" not in config.RUNTIME_OVERRIDES
    assert config.MAX_CONCURRENT == env_default_max
    # And it's gone from disk too.
    saved = json.loads(config.OVERRIDES_FILE.read_text())
    assert "max_concurrent" not in saved


def test_load_overrides_reads_disk_and_applies():
    # Write a doctored override file then load it.
    config.OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.OVERRIDES_FILE.write_text(json.dumps({"max_concurrent": 4, "min_dispatch_gap_ms": 100}))
    config.RUNTIME_OVERRIDES.clear()
    config.load_overrides()
    assert config.RUNTIME_OVERRIDES["max_concurrent"] == 4
    assert config.MAX_CONCURRENT == 4
    assert config.MIN_DISPATCH_GAP_S == pytest.approx(0.1)


def test_load_overrides_ignores_unknown_keys_without_crashing():
    config.OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.OVERRIDES_FILE.write_text(json.dumps({"max_concurrent": 4, "ghost_knob": 99}))
    config.load_overrides()
    assert "ghost_knob" not in config.RUNTIME_OVERRIDES
    assert config.RUNTIME_OVERRIDES["max_concurrent"] == 4


def test_load_overrides_ignores_out_of_range_values():
    config.OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.OVERRIDES_FILE.write_text(json.dumps({"max_concurrent": 99999}))
    config.load_overrides()
    assert "max_concurrent" not in config.RUNTIME_OVERRIDES
    # Module attr stays at the env default.
    assert config.MAX_CONCURRENT == config.ENV_DEFAULTS["max_concurrent"]


def test_knob_snapshot_has_every_editable_knob():
    rows = config.knob_snapshot()
    assert len(rows) == len(config.EDITABLE_KNOBS)
    keys = {r["key"] for r in rows}
    assert keys == set(config.EDITABLE_KNOBS.keys())
    for r in rows:
        assert "label" in r and "value" in r and "default" in r and "override" in r


def test_knob_snapshot_marks_overridden_rows():
    config.set_override("max_concurrent", "8")
    rows = {r["key"]: r for r in config.knob_snapshot()}
    assert rows["max_concurrent"]["override"] is True
    assert rows["min_dispatch_gap_ms"]["override"] is False


def test_storm_warn_retries_default_is_25():
    # PR #037 observability: env-derived default for the storm early-warning
    # threshold. Overridable via THROTTLE_STORM_WARN_RETRIES.
    assert config.STORM_WARN_RETRIES == 25
    assert isinstance(config.STORM_WARN_RETRIES, int)
