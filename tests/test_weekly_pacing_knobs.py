"""Weekly pacing mode and law are hot-tunable via /ui/config (finite choices).

The env vars stay the boot default; a runtime override takes effect on the next
sample or pick because the hot path reads ``config.WEEKLY_PACING_MODE`` /
``config.WEEKLY_PACING_LAW`` at call time.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import config, proxy
from anthropic_throttle_proxy.ui.routes import attach_ui


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OVERRIDES_FILE", tmp_path / "overrides.json")
    config.RUNTIME_OVERRIDES.clear()
    config.ENV_DEFAULTS.clear()
    config._capture_env_defaults()
    monkeypatch.setattr(config, "WEEKLY_PACING_MODE", "off")
    monkeypatch.setattr(config, "WEEKLY_PACING_LAW", "line")
    yield
    for key in list(config.RUNTIME_OVERRIDES):
        config.reset_override(key)
    config.RUNTIME_OVERRIDES.clear()


def test_set_override_flips_mode_and_law():
    assert config.set_override("weekly_pacing_mode", " Enforce ") == "enforce"
    assert config.WEEKLY_PACING_MODE == "enforce"
    assert config.set_override("weekly_pacing_law", "remaining") == "remaining"
    assert config.WEEKLY_PACING_LAW == "remaining"


@pytest.mark.parametrize(
    ("key", "bad"),
    [("weekly_pacing_mode", "bogus"), ("weekly_pacing_mode", ""), ("weekly_pacing_law", "fast")],
)
def test_invalid_choice_is_refused_and_nothing_changes(key, bad):
    attr = "WEEKLY_PACING_MODE" if key == "weekly_pacing_mode" else "WEEKLY_PACING_LAW"
    live = getattr(config, attr)
    with pytest.raises(ValueError):
        config.set_override(key, bad)
    assert getattr(config, attr) == live
    assert key not in config.RUNTIME_OVERRIDES
    assert not config.OVERRIDES_FILE.exists()


def test_picker_sees_the_hot_mode():
    """The pick reads the same attribute the override writes, with no restart."""
    snapshot = [{"bearer_id": "a", "name": "gmail"}]
    now = time.time()
    # Observe never defers: the filter returns the snapshot untouched.
    config.set_override("weekly_pacing_mode", "observe")
    assert proxy._weekly_pacing_filter(snapshot, now) is snapshot
    # Mode back to off is also a no-op, and the override is what the pick saw.
    config.set_override("weekly_pacing_mode", "off")
    assert proxy._weekly_pacing_filter(snapshot, now) is snapshot
    assert config.WEEKLY_PACING_MODE == "off"


async def _client() -> TestClient:
    app = web.Application()
    attach_ui(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_post_ui_config_sets_mode_and_refuses_bad_value_with_400():
    client = await _client()
    try:
        ok = await client.post("/ui/config", data={"key": "weekly_pacing_mode", "value": "observe"})
        assert ok.status == 200
        assert config.WEEKLY_PACING_MODE == "observe"

        bad = await client.post("/ui/config", data={"key": "weekly_pacing_mode", "value": "bogus"})
        assert bad.status == 400
        assert config.WEEKLY_PACING_MODE == "observe"
    finally:
        await client.close()


async def test_htmx_bad_value_keeps_200_with_inline_error():
    client = await _client()
    try:
        resp = await client.post(
            "/ui/config",
            data={"key": "weekly_pacing_law", "value": "fast"},
            headers={"HX-Request": "true"},
        )
        assert resp.status == 200
        assert config.WEEKLY_PACING_LAW == "line"
        assert "invalid value" in await resp.text()
    finally:
        await client.close()
