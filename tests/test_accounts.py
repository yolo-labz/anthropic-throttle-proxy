"""Unit tests for the per-account dashboard mapping (accounts.py).

Covers the env-spec parser, credential-file digestion (hash must match
``ratelimit._bearer_id`` for the same token), the mtime cache, the pure
pace/ETA math, and the merged account view the /ui template renders.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC

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
    accounts._endpoint_cache.clear()
    accounts._email_cache.clear()
    accounts._local_identity_cache.clear()
    accounts._endpoint_locks.clear()
    yield
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    accounts._email_cache.clear()
    accounts._local_identity_cache.clear()
    accounts._endpoint_locks.clear()


def _write_account(base, sub: str, token: str, email: str | None, expires_at_ms=None):
    """Write a full account dir: <base>/<sub>/{.credentials.json,.claude.json}.

    Returns the credentials path. ``email`` is the locally-persisted identity
    (``oauthAccount.emailAddress``); None writes no identity file (a dead account
    whose email is unknown to both the network and the local fallback).
    """
    acct_dir = base / sub
    acct_dir.mkdir(parents=True, exist_ok=True)
    cred = acct_dir / ".credentials.json"
    _write_cred(cred, token, expires_at_ms=expires_at_ms)
    if email is not None:
        (acct_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email}})
        )
    return cred


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test may reach the real OAuth endpoints — default to transport error
    (the panel then renders exactly the pre-#55 bearer view)."""

    async def _offline(url: str, token: str):
        return 0, None

    monkeypatch.setattr(accounts, "_get_json", _offline)


def _stub_endpoint(monkeypatch, responses: dict[str, dict[str, tuple[int, dict | None]]]):
    """Route fake endpoint responses by token: {token: {"usage"|"profile": (status, body)}}."""

    async def fake(url: str, token: str):
        kind = "usage" if "usage" in url else "profile"
        return responses.get(token, {}).get(kind, (0, None))

    monkeypatch.setattr(accounts, "_get_json", fake)


def _iso(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _usage_body(
    u5: float = 40.0,
    u7: float = 84.0,
    r5: float = NOW + 3600,
    r7: float = NOW + 3 * DAY,
    **buckets,
) -> dict:
    body = {
        "five_hour": {"utilization": u5, "resets_at": _iso(r5)},
        "seven_day": {"utilization": u7, "resets_at": _iso(r7)},
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "iguana_necktie": None,  # schema-churn junk the parser must ignore
        "extra_usage": None,
    }
    body.update(buckets)
    return body


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


def test_routing_snapshot_returns_usable_tokens_only(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh.json"
    expired = tmp_path / "expired.json"
    malformed = tmp_path / "malformed.json"
    fresh_value = "tok-" + "fresh"
    _write_cred(fresh, fresh_value, expires_at_ms=int((NOW + 3600) * 1000))
    _write_cred(expired, "tok-expired", expires_at_ms=int((NOW - 1) * 1000))
    malformed.write_text(json.dumps({"claudeAiOauth": {}}))
    monkeypatch.setattr(
        config,
        "ACCOUNT_CRED_PATHS",
        f"F:{fresh},X:{expired},BAD:{malformed},GONE:{tmp_path / 'nope.json'}",
    )

    (route,) = accounts.routing_snapshot(NOW)

    assert route["label"] == "F"
    assert route["bearer_id"] == _expected_bid(fresh_value)
    assert route["token"] == fresh_value
    assert "token" not in accounts.account_snapshot()[0]


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

    assert "Accounts ·" in html
    assert '<span class="count">1</span>' in html  # count rendered in the styled span
    assert html.count(bid) >= 2  # accounts row + labelled bearer row
    assert ">A</span>" in html  # account label chip in both tables
    assert "62%" in html
    assert token not in html  # raw token must never reach the page


async def test_ui_stats_hides_panel_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    html = await _render_stats()
    assert "Accounts ·" not in html


# ── endpoint truth (PR #55) ─────────────────────────────────────────────


def test_parse_usage_percent_iso_and_nulls():
    body = _usage_body(
        u5=97.0,
        u7=59.0,
        seven_day_opus={"utilization": 12.5, "resets_at": _iso(NOW + 2 * DAY)},
        extra_usage={"is_enabled": True, "used_credits": 3.5, "currency": "BRL"},
    )
    usage = accounts._parse_usage(body)
    assert usage["util_5h"] == pytest.approx(0.97)  # percent → fraction
    assert usage["util_7d"] == pytest.approx(0.59)
    assert usage["reset_5h"] == int(NOW + 3600)
    assert usage["sonnet"] is None  # null bucket tolerated
    assert usage["opus"]["util"] == pytest.approx(0.125)
    assert usage["extra"] == {"used": 3.5, "currency": "BRL"}


def test_parse_usage_garbage_tolerated():
    usage = accounts._parse_usage(
        {"five_hour": {"utilization": "97", "resets_at": 12345}, "seven_day": "nope"}
    )
    assert usage["util_5h"] is None  # string percent rejected, not crashed
    assert usage["reset_5h"] is None
    assert usage["util_7d"] is None
    assert usage["extra"] is None


async def test_endpoint_overrides_stale_bearer(tmp_path, monkeypatch):
    # The 10/06 blind spot in panel form: the proxy's last-seen snapshot says
    # 30%/45% while the account is REALLY at 97%/59% — endpoint truth wins.
    token = "tok-live"  # noqa: S105
    cred = tmp_path / "a.json"
    _write_cred(cred, token, expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    _stub_endpoint(
        monkeypatch,
        {
            token: {
                "usage": (200, _usage_body(u5=97.0, u7=59.0)),
                "profile": (200, {"account": {"email": "a@x"}}),
            }
        },
    )
    endpoint = await accounts.refresh_endpoint(NOW)
    bearers = [
        {
            "bearer_id": _expected_bid(token),
            "unified": {
                "util_5h": 0.30,
                "reset_5h": int(NOW + 4000),
                "status_5h": "allowed",
                "util_7d": 0.45,
                "reset_7d": int(NOW + 3 * DAY),
                "status_7d": "allowed",
            },
        }
    ]
    (view,) = accounts.account_view(bearers, NOW, endpoint)
    assert view["src"] == "endpoint"
    assert view["win5"]["pct"] == 97
    assert view["win7"]["pct"] == 59
    assert view["email"] == "a@x"


async def test_endpoint_stale_falls_back_to_bearer(tmp_path, monkeypatch):
    token = "tok-stale"  # noqa: S105
    cred = tmp_path / "a.json"
    _write_cred(cred, token)
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    accounts._endpoint_cache[str(cred)] = {
        "fetched": NOW - accounts.ENDPOINT_STALE_MAX_S - 1,
        "usage": accounts._parse_usage(_usage_body(u5=97.0)),
        "err": None,
    }
    bearers = [
        {
            "bearer_id": _expected_bid(token),
            "unified": {"util_5h": 0.30, "reset_5h": int(NOW + 4000), "status_5h": "allowed"},
        }
    ]
    (view,) = accounts.account_view(bearers, NOW, dict(accounts._endpoint_cache))
    assert view["src"] == "proxy"  # stale endpoint reading no longer overrides
    assert view["win5"]["pct"] == 30


async def test_endpoint_401_surfaces_and_drops_usage(tmp_path, monkeypatch):
    token = "tok-dead"  # noqa: S105
    cred = tmp_path / "a.json"
    _write_cred(cred, token)
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    _stub_endpoint(monkeypatch, {token: {"usage": (401, None)}})
    endpoint = await accounts.refresh_endpoint(NOW)
    (view,) = accounts.account_view([], NOW, endpoint)
    assert view["src"] == "none"
    assert "401" in view["endpoint_err"]


async def test_endpoint_rejected_styling_at_cap(tmp_path, monkeypatch):
    token = "tok-cap"  # noqa: S105
    cred = tmp_path / "a.json"
    _write_cred(cred, token)
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    _stub_endpoint(monkeypatch, {token: {"usage": (200, _usage_body(u5=100.0, u7=80.0))}})
    endpoint = await accounts.refresh_endpoint(NOW)
    (view,) = accounts.account_view([], NOW, endpoint)
    assert view["win5"]["pct"] == 100
    assert view["win5"]["rejected"] is True


async def test_email_cache_invalidated_on_cred_rewrite(tmp_path, monkeypatch):
    # The 11/06 contamination vector: a /login rewrites the credential file.
    # The email must follow the FILE, not a time-based cache.
    cred = tmp_path / "a.json"
    _write_cred(cred, "tok-one")
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    _stub_endpoint(
        monkeypatch,
        {
            "tok-one": {
                "usage": (200, _usage_body()),
                "profile": (200, {"account": {"email": "one@x"}}),
            },
            "tok-two": {
                "usage": (200, _usage_body()),
                "profile": (200, {"account": {"email": "two@x"}}),
            },
        },
    )
    await accounts.refresh_endpoint(NOW)
    assert accounts.account_email(str(cred)) == "one@x"
    _write_cred(cred, "tok-two")
    os.utime(cred, ns=(os.stat(cred).st_mtime_ns + 1, os.stat(cred).st_mtime_ns + 1))
    accounts._endpoint_cache.clear()  # force the next refresh past the TTL
    await accounts.refresh_endpoint(NOW + accounts.ENDPOINT_TTL_S + 1)
    assert accounts.account_email(str(cred)) == "two@x"


def test_identity_state_verdicts():
    collapsed = accounts.identity_state([{"email": "x@y"}, {"email": "x@y"}])
    assert collapsed["collapsed"] is True and collapsed["email"] == "x@y"
    distinct = accounts.identity_state([{"email": "a@y"}, {"email": "b@y"}])
    assert distinct["collapsed"] is False
    unknown = accounts.identity_state([{"email": None}, {"email": "a@y"}])
    assert unknown["collapsed"] is False and unknown["known"] == 1


async def test_ui_stats_renders_identity_banner(tmp_path, monkeypatch):
    # Both credential files hold the SAME account → the panel must say so
    # loudly (the live 11/06 failure was invisible in every surface).
    import time as _time

    now = _time.time()
    cred_a, cred_b = tmp_path / "a.json", tmp_path / "b.json"
    _write_cred(cred_a, "tok-a", expires_at_ms=int((now + 3600) * 1000))
    _write_cred(cred_b, "tok-b", expires_at_ms=int((now + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred_a},B:{cred_b}")
    same = {"account": {"email": "same@x"}}
    _stub_endpoint(
        monkeypatch,
        {
            "tok-a": {
                "usage": (200, _usage_body(r5=now + 3600, r7=now + 3 * DAY)),
                "profile": (200, same),
            },
            "tok-b": {
                "usage": (200, _usage_body(r5=now + 3600, r7=now + 3 * DAY)),
                "profile": (200, same),
            },
        },
    )
    html = await _render_stats()
    assert "accounts collapsed" in html
    assert "same@x" in html
    assert 'class="src src-endpoint"' in html


# ── FR-005 distinctness guard: local identity fallback + partial collision ──


def test_account_email_falls_back_to_local_identity(tmp_path):
    # Dead account: profile fetch offline (autouse _no_network → status 0) so
    # the NETWORK email is unknown. The local identity (.claude.json) must still
    # resolve — the 09/07 case where the collision was invisible precisely
    # because every token was dead and only the network path knew the email.
    cred = _write_account(
        tmp_path, "a", "tok-dead", email="pedro@pm.me", expires_at_ms=int((NOW - 1) * 1000)
    )
    assert accounts._email_cache.get(str(cred)) is None  # no network email cached
    assert accounts.account_email(str(cred)) == "pedro@pm.me"


def test_local_identity_absent_without_claude_json(tmp_path):
    cred = _write_account(tmp_path, "a", "tok", email=None)  # no .claude.json written
    assert accounts.account_email(str(cred)) is None


def test_local_identity_invalidates_on_identity_file_change(tmp_path):
    cred = _write_account(tmp_path, "a", "tok", email="one@pm.me")
    assert accounts.account_email(str(cred)) == "one@pm.me"  # reads + caches
    assert accounts.account_email(str(cred)) == "one@pm.me"  # cache hit, unchanged
    # A change to the identity file bumps its mtime → the cache (keyed on BOTH
    # mtimes) invalidates and the corrected identity is picked up. The guard is
    # a correctness surface, so it must never keep serving a stale email
    # (adversarial-review MEDIUM #1).
    ident = tmp_path / "a" / ".claude.json"
    ident.write_text(json.dumps({"oauthAccount": {"emailAddress": "two@pm.me"}}))
    os.utime(ident, ns=(0, os.stat(ident).st_mtime_ns + 1_000_000_000))
    assert accounts.account_email(str(cred)) == "two@pm.me"


def test_identity_state_detects_partial_collision_via_local(tmp_path, monkeypatch):
    # The exact 09/07 outage shape: A and B are the SAME account (pm.me), C is
    # distinct, and ALL tokens are expired (network identity unavailable). The
    # binary "collapsed" flag reads False (not ALL same), but the collision that
    # mutually revokes A+B must still be caught via duplicates.
    a = _write_account(
        tmp_path, "a", "tok-a", email="dup@pm.me", expires_at_ms=int((NOW - 1) * 1000)
    )
    b = _write_account(
        tmp_path, "b", "tok-b", email="dup@pm.me", expires_at_ms=int((NOW - 1) * 1000)
    )
    c = _write_account(
        tmp_path, "c", "tok-c", email="solo@x.com", expires_at_ms=int((NOW - 1) * 1000)
    )
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{a},B:{b},C:{c}")

    view = [
        {"label": s["label"], "email": accounts.account_email(s["path"])}
        for s in accounts.account_snapshot()
    ]
    verdict = accounts.identity_state(view)

    assert verdict["collapsed"] is False  # C is distinct → not fully collapsed
    assert verdict["distinct"] == 2
    assert verdict["duplicates"] == {"dup@pm.me": ["A", "B"]}
    assert verdict["known"] == 3


def test_account_email_ignores_stale_network_cache_after_relogin(tmp_path):
    # Codex MAJOR: a network email cached against an OLD credential mtime (before
    # a re-login rewrote the file) must NOT be served — fall back to the fresh
    # local identity so the guard can't miss/false-report a collision.
    cred = _write_account(tmp_path, "a", "tok", email="local-current@pm.me")
    accounts._email_cache[str(cred)] = (123, "stale-network@old.com")  # bogus old mtime
    assert accounts.account_email(str(cred)) == "local-current@pm.me"
    assert str(cred) not in accounts._email_cache  # stale entry dropped


def test_account_email_trusts_network_cache_on_mtime_match(tmp_path):
    cred = _write_account(tmp_path, "a", "tok", email="local@pm.me")
    mt = os.stat(cred).st_mtime_ns
    accounts._email_cache[str(cred)] = (mt, "network@pm.me")
    assert accounts.account_email(str(cred)) == "network@pm.me"  # fresh → authoritative


# ── poller routes through the proxy (087): avoid the direct-call self-429 ──


def test_oauth_base_routes_via_proxy_when_accounts_configured(monkeypatch):
    # A local tier with accounts must poll usage/profile through its OWN
    # loopback so the GET shares the per-bearer semaphore with real traffic —
    # a direct call bursts alongside it and trips the concurrency 429.
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/x/c.json")
    monkeypatch.setattr(config, "LISTEN_PORT", 8765)
    assert accounts._oauth_base() == "http://127.0.0.1:8765"
    assert accounts._oauth_base() + accounts._USAGE_PATH == "http://127.0.0.1:8765/api/oauth/usage"


def test_oauth_base_direct_when_no_accounts(monkeypatch):
    # Central tier (no accounts) has no local semaphore to gain → direct.
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    assert accounts._oauth_base() == "https://api.anthropic.com"


async def test_refresh_endpoint_polls_via_loopback(monkeypatch, tmp_path):
    # Codex test-gap: lock the WIRING, not just _oauth_base in isolation — the
    # poll must actually hit the loopback (127.0.0.1) not direct anthropic.com.
    cred = tmp_path / "c.json"
    _write_cred(cred, "tok-x", expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "LISTEN_PORT", 8765)
    seen: list[str] = []

    async def _capture(url, token):
        seen.append(url)
        return 0, None  # transport error → _refresh_one handles gracefully

    monkeypatch.setattr(accounts, "_get_json", _capture)
    await accounts.refresh_endpoint(NOW)
    # Exact match (not substring/startswith on a URL — CodeQL
    # py/incomplete-url-substring-sanitization): the poll hits ONLY the
    # loopback usage endpoint, never direct anthropic.com.
    assert seen == ["http://127.0.0.1:8765/api/oauth/usage"]


# ── spec 2: limits[] scoped per-model bucket capture ──────────────────────


def test_parse_usage_captures_limits_and_scoped():
    body = {
        "five_hour": {"utilization": 99.0, "resets_at": _iso(NOW + 3600)},
        "seven_day": {"utilization": 20.0, "resets_at": _iso(NOW + 3 * DAY)},
        "seven_day_sonnet": None,
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 99,
                "severity": "critical",
                "is_active": True,
                "scope": None,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 20,
                "severity": "normal",
                "is_active": False,
                "scope": None,
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 5,
                "severity": "normal",
                "is_active": False,
                "scope": {"model": {"id": None, "display_name": "Fable"}},
            },
        ],
    }
    parsed = accounts._parse_usage(body)
    assert len(parsed["limits"]) == 3
    session = parsed["limits"][0]
    assert session["kind"] == "session"
    assert session["severity"] == "critical" and session["is_active"] is True
    assert abs(session["util"] - 0.99) < 1e-9
    scoped = parsed["scoped"]
    assert scoped is not None and scoped["kind"] == "weekly_scoped"
    assert scoped["model"] == "Fable" and abs(scoped["util"] - 0.05) < 1e-9


def test_parse_usage_limits_absent_or_malformed():
    empty = accounts._parse_usage({})
    assert empty["limits"] == [] and empty["scoped"] is None
    body = {
        "limits": [
            "not-a-dict",
            {
                "kind": "weekly_scoped",
                "percent": 50,
                "scope": {"model": {"display_name": "Sonnet"}},
            },
        ]
    }
    parsed = accounts._parse_usage(body)
    assert len(parsed["limits"]) == 1  # malformed skipped
    assert parsed["scoped"]["model"] == "Sonnet"
    assert abs(parsed["scoped"]["util"] - 0.5) < 1e-9
