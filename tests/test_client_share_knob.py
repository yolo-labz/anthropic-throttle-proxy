"""Client shares are hot-tunable through /ui/config (spec 377 phase 2 enforcement).

The env var stays the boot default; a runtime override takes effect on the next
pick because ``proxy._weekly_share_defer`` reads ``config.CLIENT_SHARES`` at call
time, and ``/__throttle/admission`` publishes that same dict — no second source
of truth. A malformed value is refused with 400 and nothing changes, so a save
can never silently delete a workspace's share.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import accounts, config, proxy
from anthropic_throttle_proxy import usage_allocator as ua
from anthropic_throttle_proxy.ui.routes import attach_ui

WEEK = ua.WEEK_S


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OVERRIDES_FILE", tmp_path / "overrides.json")
    config.RUNTIME_OVERRIDES.clear()
    config.ENV_DEFAULTS.clear()
    monkeypatch.setattr(config, "CLIENT_SHARES", {})
    config._capture_env_defaults()
    yield
    for key in list(config.RUNTIME_OVERRIDES):
        config.reset_override(key)
    config.RUNTIME_OVERRIDES.clear()


def _ahead_fleet(monkeypatch: pytest.MonkeyPatch) -> float:
    """Two accounts running ahead of schedule, with side-projects over its share.

    gmail 0.60 + proton 0.47 of a half-elapsed week → Σutil 1.07 over Σelapsed
    1.0, so the fleet is ahead and the share rule is allowed to defer.
    """
    view = [
        {"label": "gmail", "bearer_id": "acct-a"},
        {"label": "proton", "bearer_id": "acct-b"},
    ]
    for key, value in (
        ("WEEKLY_BURST", ua.DEFAULT_BURST),
        ("QUEUE_RETRY_AFTER_MAX_S", 10**9),
        ("WEEKLY_PACING_MODE", "enforce"),
    ):
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(proxy, "_account_routing_enabled", lambda: True)
    monkeypatch.setattr(accounts, "routing_snapshot", lambda _now=None: list(view))
    tokens = ua.WeeklyTokens()
    monkeypatch.setattr(proxy, "_WEEKLY_TOKENS", tokens)
    now = time.time()
    reset = now + (1.0 - 0.5) * WEEK
    config.bearer_state.clear()
    config.bearer_state["acct-a"] = {"unified": {"util_7d": 0.60, "reset_7d": reset}}
    config.bearer_state["acct-b"] = {"unified": {"util_7d": 0.47, "reset_7d": reset}}
    tokens.add("side-projects", 900, now)
    tokens.add("other", 100, now)
    return now


async def _client() -> TestClient:
    app = web.Application()
    attach_ui(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_knob_shows_the_canonical_shares_text():
    config.set_override("client_shares", " side-projects:0.4 , other:0.25 ")
    rows = {row["key"]: row for row in config.knob_snapshot()}
    assert rows["client_shares"]["value"] == "side-projects:0.4,other:0.25"
    assert rows["client_shares"]["override"] is True


def test_empty_value_is_the_valid_no_shares_state():
    config.set_override("client_shares", "side-projects:0.4")
    assert config.set_override("client_shares", "   ") == ""
    assert config.CLIENT_SHARES == {}


@pytest.mark.parametrize(
    "bad",
    ["side-projects", "side-projects:2", "side-projects:0", ":0.4", "ok:0.2,bad", "x:nan"],
)
def test_set_override_refuses_malformed_shares(bad: str):
    config.set_override("client_shares", "side-projects:0.4")
    with pytest.raises(ValueError):
        config.set_override("client_shares", bad)
    assert config.CLIENT_SHARES == {"side-projects": 0.4}
    assert config.RUNTIME_OVERRIDES["client_shares"] == "side-projects:0.4"


async def test_post_ui_config_sets_shares_and_the_next_pick_sees_it(monkeypatch):
    client = await _client()
    try:
        ok = await client.post(
            "/ui/config",
            data={"key": "client_shares", "value": " side-projects:0.4 , other:0.3 "},
        )
        assert ok.status == 200
        assert config.CLIENT_SHARES == {"side-projects": 0.4, "other": 0.3}

        _ahead_fleet(monkeypatch)
        retry = proxy._weekly_share_defer("side-projects", priority=False)
        assert isinstance(retry, int) and 1 <= retry <= 10**9

        view = proxy._weekly_pacing_view()
        assert view["client_shares"]["configured"] == {"side-projects": 0.4, "other": 0.3}
    finally:
        await client.close()


async def test_invalid_shares_are_refused_with_400_and_the_previous_value_survives():
    client = await _client()
    try:
        ok = await client.post(
            "/ui/config", data={"key": "client_shares", "value": "side-projects:0.4"}
        )
        assert ok.status == 200
        for bad in ("side-projects", "side-projects:2", ":0.4", "ok:0.2,bad", "x:nan"):
            resp = await client.post("/ui/config", data={"key": "client_shares", "value": bad})
            assert resp.status == 400, bad
            assert config.CLIENT_SHARES == {"side-projects": 0.4}
            assert config.RUNTIME_OVERRIDES["client_shares"] == "side-projects:0.4"
    finally:
        await client.close()


def test_share_limited_client_defers_while_an_unshared_client_routes(monkeypatch):
    config.set_override("client_shares", "side-projects:0.4,other:0.3")
    _ahead_fleet(monkeypatch)
    assert isinstance(proxy._weekly_share_defer("side-projects", priority=False), int)
    # "home" carries no share → never share-deferred; "other" is under its share.
    assert proxy._weekly_share_defer("home", priority=False) is None
    assert proxy._weekly_share_defer("other", priority=False) is None
