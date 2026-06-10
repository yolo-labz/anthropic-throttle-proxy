"""Unit tests for the per-account dashboard mapping (accounts.py).

Covers the env-spec parser, credential-file digestion (hash must match
``ratelimit._bearer_id`` for the same token), the mtime cache, the pure
pace/ETA math, and the merged account view the /ui template renders.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from anthropic_throttle_proxy import accounts, config
from anthropic_throttle_proxy.ratelimit import _bearer_id

NOW = 1_781_120_000.0
DAY = 86_400


def _expected_bid(token: str) -> str:
    return hashlib.sha256(f"Bearer {token}".encode()).hexdigest()[:8]


def _write_cred(path, token: str, expires_at_ms: int | None = None) -> None:
    oauth: dict = {"accessToken": token, "refreshToken": "never-read"}
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


@pytest.fixture(autouse=True)
def _clean_cache():
    accounts._cache.clear()
    yield
    accounts._cache.clear()


# ── parse_spec ──────────────────────────────────────────────────────────


def test_parse_spec_empty_and_valid():
    assert accounts.parse_spec("") == []
    assert accounts.parse_spec("A:/x/c.json,B:/y/c.json") == [
        ("A", "/x/c.json"),
        ("B", "/y/c.json"),
    ]


def test_parse_spec_skips_malformed_and_duplicates():
    raw = "no-colon, :/path-only,label:, A:/first , A:/second ,B:/ok"
    assert accounts.parse_spec(raw) == [("A", "/first"), ("B", "/ok")]


# ── account_snapshot ────────────────────────────────────────────────────


def test_snapshot_digest_matches_bearer_id(tmp_path, monkeypatch):
    token = "sk-ant-oat01-fake-token-for-tests"  # noqa: S105 — test fixture, not a real credential
    cred = tmp_path / "c.json"
    _write_cred(cred, token, expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")

    (snap,) = accounts.account_snapshot()
    assert snap["label"] == "A"
    assert snap["error"] is None
    assert snap["bearer_id"] == _expected_bid(token)
    # The digest MUST equal what the proxy computes for the same header.
    assert snap["bearer_id"] == _bearer_id({"Authorization": f"Bearer {token}"})


def test_snapshot_missing_file_and_bad_json(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    no_token = tmp_path / "empty.json"
    no_token.write_text(json.dumps({"claudeAiOauth": {}}))
    spec = f"GONE:{tmp_path}/nope.json,BAD:{bad},EMPTY:{no_token}"
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", spec)

    gone, badv, empty = accounts.account_snapshot()
    assert gone["bearer_id"] is None and gone["error"] == "credentials file missing"
    assert badv["bearer_id"] is None and badv["error"] == "credentials file malformed"
    assert empty["bearer_id"] is None and empty["error"] == "no access token in credentials"


def test_snapshot_mtime_cache_refreshes_on_rotation(tmp_path, monkeypatch):
    cred = tmp_path / "c.json"
    _write_cred(cred, "token-one")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")

    first = accounts.account_snapshot()[0]["bearer_id"]
    assert first == _expected_bid("token-one")

    # Simulate the ~8h access-token rotation: same path, new content+mtime.
    _write_cred(cred, "token-two-after-rotation")
    st = cred.stat()
    os.utime(cred, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    second = accounts.account_snapshot()[0]["bearer_id"]
    assert second == _expected_bid("token-two-after-rotation")
    assert second != first


# ── pace / ETA math ─────────────────────────────────────────────────────


def test_pace_eta_midcycle_over_budget():
    # Half the 7d cycle elapsed, 60% burned → pace 1.2×, exhausts before reset.
    reset = int(NOW + 3.5 * DAY)
    pace, eta = accounts._pace_eta(0.6, reset, NOW)
    assert pace == 1.2
    assert eta is not None  # 3.5d/0.6 ≈ 5.83d from cycle start < 7d


def test_pace_eta_on_budget_has_no_eta():
    reset = int(NOW + 3.5 * DAY)
    pace, eta = accounts._pace_eta(0.4, reset, NOW)
    assert pace == 0.8
    assert eta is None  # exhaustion would land after the reset → on budget


def test_pace_eta_guards():
    assert accounts._pace_eta(None, int(NOW + DAY), NOW) == (None, None)
    assert accounts._pace_eta(0.5, None, NOW) == (None, None)
    assert accounts._pace_eta(0.5, int(NOW - 10), NOW) == (None, None)  # reset passed
    # Cycle just started: elapsed below the noise floor.
    fresh_reset = int(NOW + accounts.WINDOW_7D_S - 60)
    assert accounts._pace_eta(0.5, fresh_reset, NOW) == (None, None)
    assert accounts._pace_eta(0.0, int(NOW + 3 * DAY), NOW) == (None, None)


# ── window / token views ────────────────────────────────────────────────


def test_window_view_stale_zeroes_utilization():
    # Last-seen reading predates the window reset → show 0%, not the stale peak.
    w = accounts._window_view(0.97, int(NOW - 100), "rejected", NOW)
    assert w == {"pct": 0, "stale": True, "rejected": False, "reset_in": None}


def test_window_view_live_and_rejected():
    w = accounts._window_view(0.42, int(NOW + 7200), "allowed", NOW)
    assert w["pct"] == 42 and not w["stale"] and not w["rejected"]
    assert w["reset_in"] == "2h 00m"
    r = accounts._window_view(1.0, int(NOW + 60), "rejected", NOW)
    assert r["rejected"] is True and r["pct"] == 100


def test_window_view_absent():
    assert accounts._window_view(None, None, None, NOW) is None


def test_token_view_states():
    fresh = accounts._token_view(int((NOW + 6 * 3600) * 1000), NOW)
    assert fresh["state"] == "fresh" and "expires in 6h" in fresh["detail"]
    recent = accounts._token_view(int((NOW - 30 * 60) * 1000), NOW)
    assert recent["state"] == "expiring"
    dead = accounts._token_view(int((NOW - 9 * 3600) * 1000), NOW)
    assert dead["state"] == "expired" and "refresh pipeline" in dead["detail"]
    assert accounts._token_view(None, NOW) is None


# ── merged account view ─────────────────────────────────────────────────


def test_account_view_merges_live_bearer(tmp_path, monkeypatch):
    token = "tok-a"  # noqa: S105 — test fixture, not a real credential
    cred = tmp_path / "a.json"
    _write_cred(cred, token, expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")

    bid = _expected_bid(token)
    bearers = [
        {
            "bearer_id": bid,
            "unified": {
                "util_5h": 0.62,
                "reset_5h": int(NOW + 4000),
                "status_5h": "allowed",
                "util_7d": 0.6,
                "reset_7d": int(NOW + 3.5 * DAY),
                "status_7d": "allowed",
            },
        }
    ]
    (view,) = accounts.account_view(bearers, NOW)
    assert view["seen"] is True
    assert view["win5"]["pct"] == 62
    assert view["win7"]["pct"] == 60
    assert view["pace"] == 1.2 and view["pace_warn"] is True
    assert view["eta"] is not None
    assert view["token"]["state"] == "fresh"


def test_account_view_unseen_bearer_still_renders(tmp_path, monkeypatch):
    # The 10/06 blind spot: B parked → proxy never saw its bearer. The panel
    # must still show the account (seen=False) instead of hiding it.
    cred = tmp_path / "b.json"
    _write_cred(cred, "tok-b", expires_at_ms=int((NOW - 9 * 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"B:{cred}")

    (view,) = accounts.account_view([], NOW)
    assert view["seen"] is False
    assert view["win5"] is None and view["win7"] is None
    assert view["pace"] is None and view["eta"] is None
    assert view["token"]["state"] == "expired"


def test_bearer_labels_reverse_map(tmp_path, monkeypatch):
    cred_a = tmp_path / "a.json"
    cred_b = tmp_path / "b.json"
    _write_cred(cred_a, "tok-a")
    _write_cred(cred_b, "tok-b")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred_a},B:{cred_b},GONE:/nope")

    labels = accounts.bearer_labels()
    assert labels == {_expected_bid("tok-a"): "A", _expected_bid("tok-b"): "B"}


def test_unconfigured_is_invisible(monkeypatch):
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    assert accounts.account_snapshot() == []
    assert accounts.account_view([], NOW) == []
    assert accounts.bearer_labels() == {}


# ── rendered dashboard ──────────────────────────────────────────────────


async def _render_stats() -> str:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from anthropic_throttle_proxy.ui.routes import attach_ui

    app = web.Application()
    attach_ui(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/ui/stats")
        assert resp.status == 200
        return await resp.text()
    finally:
        await client.close()


async def test_ui_stats_renders_accounts_panel(tmp_path, monkeypatch):
    import time as _time

    from anthropic_throttle_proxy import proxy

    token = "tok-render"  # noqa: S105 — test fixture, not a real credential
    cred = tmp_path / "a.json"
    now = _time.time()
    _write_cred(cred, token, expires_at_ms=int((now + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")

    bid = _expected_bid(token)
    proxy.bearer_state[bid] = {
        "inflight": 0,
        "queued": 0,
        "served": 3,
        "last_ratelimit": None,
        "unified": {
            "util_5h": 0.62,
            "reset_5h": int(now + 4000),
            "status_5h": "allowed",
            "util_7d": 0.31,
            "reset_7d": int(now + 3 * DAY),
            "status_7d": "allowed",
        },
    }
    try:
        html = await _render_stats()
    finally:
        proxy.bearer_state.pop(bid, None)

    assert "Accounts · 1" in html
    assert html.count(bid) >= 2  # accounts row + labelled bearer row
    assert ">A</span>" in html  # account label chip in both tables
    assert "62%" in html
    assert token not in html  # raw token must never reach the page


async def test_ui_stats_hides_panel_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    html = await _render_stats()
    assert "Accounts ·" not in html
