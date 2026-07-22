"""Unit tests for the per-account dashboard mapping (accounts.py).

Covers the env-spec parser, credential-file digestion (hash must match
``ratelimit._bearer_id`` for the same token), the mtime cache, the pure
pace/ETA math, and the merged account view the /ui template renders.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC

import pytest

from anthropic_throttle_proxy import accounts, config, limiter
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


def _clear_account_module_state() -> None:
    accounts._cache.clear()
    accounts._endpoint_cache.clear()
    accounts._email_cache.clear()
    accounts._local_identity_cache.clear()
    accounts._endpoint_locks.clear()
    accounts._verify_locks.clear()
    accounts._endpoint_backoff.clear()
    accounts._endpoint_cache_loaded = False
    limiter._retry_after_state = None


@pytest.fixture(autouse=True)
def _clean_cache():
    _clear_account_module_state()
    yield
    _clear_account_module_state()


@pytest.fixture(autouse=True)
def _isolate_endpoint_cache_file(tmp_path, monkeypatch):
    """Point the persisted endpoint cache at a per-test tmp file so a lazy seed
    never reads the host's real ``$XDG_STATE_HOME`` copy (would poison tests)."""
    monkeypatch.setattr(config, "ENDPOINT_CACHE_FILE", tmp_path / "endpoint-cache.json")


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


# ── FR-005 verify-before-warn: email provenance + suspected collision split ──


async def test_guard_email_reports_provenance(tmp_path, monkeypatch):
    cred = _write_account(tmp_path, "a", "tok-a", email="label@pm.me")
    # Local .claude.json fallback → unverified (the label that lies post-promote).
    assert accounts.guard_email(str(cred)) == ("label@pm.me", False)
    _stub_endpoint(monkeypatch, {"tok-a": {"profile": (200, {"account": {"email": "live@pm.me"}})}})
    assert await accounts.force_verify_email(str(cred)) == "live@pm.me"
    assert accounts.guard_email(str(cred)) == ("live@pm.me", True)
    # Credential rewrite drops the verified email → back to the label, unverified.
    _write_cred(cred, "tok-b")
    os.utime(cred, ns=(0, os.stat(cred).st_mtime_ns + 1_000_000_000))
    assert accounts.guard_email(str(cred)) == ("label@pm.me", False)


async def test_force_verify_email_dead_token_returns_none(tmp_path):
    # autouse _no_network → transport error: the probe cannot confirm anything.
    cred = _write_account(tmp_path, "a", "tok-dead", email="label@pm.me")
    assert await accounts.force_verify_email(str(cred)) is None
    assert accounts.guard_email(str(cred)) == ("label@pm.me", False)


async def test_force_verify_email_missing_cred_returns_none(tmp_path):
    assert await accounts.force_verify_email(str(tmp_path / "nope.json")) is None


async def test_force_verify_email_ignores_mid_probe_rotation(tmp_path, monkeypatch):
    # Codex MAJOR (TOCTOU): a rotation landing while the profile probe is in
    # flight must NOT certify the OLD token's email as the NEW file's verified
    # identity — the guard would then trust a wrong email.
    cred = _write_account(tmp_path, "a", "tok-old", email="label@pm.me")

    async def rotate_mid_probe(url: str, token: str):
        _write_cred(cred, "tok-new")
        os.utime(cred, ns=(0, os.stat(cred).st_mtime_ns + 1_000_000_000))
        return 200, {"account": {"email": "old-token@pm.me"}}

    monkeypatch.setattr(accounts, "_get_json", rotate_mid_probe)
    assert await accounts.force_verify_email(str(cred)) is None
    assert accounts.guard_email(str(cred)) == ("label@pm.me", False)  # never verified


async def test_force_verify_email_reuses_fresh_probe(tmp_path, monkeypatch):
    # Singleflight follow-up: a probe another task just completed for the SAME
    # credential is reused, not re-fired (loopback probe budget, #87 class).
    cred = _write_account(tmp_path, "a", "tok-a", email="label@pm.me")
    calls = 0

    async def counting(url: str, token: str):
        nonlocal calls
        calls += 1
        return 200, {"account": {"email": "live@pm.me"}}

    monkeypatch.setattr(accounts, "_get_json", counting)
    assert await accounts.force_verify_email(str(cred)) == "live@pm.me"
    assert await accounts.force_verify_email(str(cred)) == "live@pm.me"
    assert calls == 1


def test_identity_state_unverified_shared_email_is_suspected_not_duplicate():
    # Promote swaps .credentials.json but NOT .claude.json → a shared email with
    # an unverified member may be a stale label (the 10/07 false alarm), so it
    # must land in suspected, never straight in duplicates.
    verdict = accounts.identity_state(
        [
            {"label": "A", "email": "dup@pm.me", "verified": True},
            {"label": "B", "email": "dup@pm.me", "verified": False},
        ]
    )
    assert verdict["duplicates"] == {}
    assert verdict["suspected"] == {"dup@pm.me": ["A", "B"]}
    # Pending verification = UNKNOWN: not collapsed even though all emails match.
    assert verdict["collapsed"] is False
    # Fully verified group → real duplicate (back-compat: missing key = verified).
    confirmed = accounts.identity_state(
        [
            {"label": "A", "email": "dup@pm.me", "verified": True},
            {"label": "B", "email": "dup@pm.me"},
        ]
    )
    assert confirmed["duplicates"] == {"dup@pm.me": ["A", "B"]}
    assert confirmed["suspected"] == {} and confirmed["collapsed"] is True


def test_publish_account_gauges_suspected_reads_unknown(monkeypatch):
    # While a suspicion is pending verification the distinct gauge must read
    # UNKNOWN (-1), not "distinct" — Grafana must not show green mid-probe.
    from anthropic_throttle_proxy import metrics as _m
    from anthropic_throttle_proxy.ui import routes as _routes

    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "")
    base = {"collapsed": False, "known": 3, "duplicates": {}}
    _routes._publish_account_gauges({}, {**base, "suspected": {"d@x": ["A", "B"]}})
    assert _m.M_ACCOUNTS_DISTINCT._value.get() == -1
    assert _m.M_ACCOUNT_SUSPECTED._value.get() == 2
    _routes._publish_account_gauges({}, {**base, "suspected": {}})
    assert _m.M_ACCOUNTS_DISTINCT._value.get() == 1
    assert _m.M_ACCOUNT_SUSPECTED._value.get() == 0


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


async def test_refresh_endpoint_backs_off_after_failed_poll(monkeypatch, tmp_path):
    """A failed usage poll backs off for ``ENDPOINT_TTL_S`` before retrying.

    Regression guard for the 10/07 self-429 loop: the failure branch used to
    leave ``fetched`` stale, so the HTMX dashboard's ~2 s auto-refresh re-polled
    a throttled account on every render — hammering the loopback usage endpoint
    (which the proxy fast-fails) and stealing slots from real traffic. The
    ``_endpoint_backoff`` gate must suppress re-polls inside the window while a
    recovering (200) poll clears it so normal cadence resumes.
    """
    cred = tmp_path / "c.json"
    _write_cred(cred, "tok-x", expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "LISTEN_PORT", 8765)

    async def _noop_email(*_a, **_k):
        return None

    monkeypatch.setattr(accounts, "_refresh_email", _noop_email)
    state = {"calls": 0, "status": 429}
    ok_body = {
        "five_hour": {"utilization": 10.0, "resets_at": _iso(NOW + 3600)},
        "seven_day": {"utilization": 10.0, "resets_at": _iso(NOW + DAY)},
    }

    async def _stub(_url, _token):
        state["calls"] += 1
        return (state["status"], ok_body if state["status"] == 200 else None)

    monkeypatch.setattr(accounts, "_get_json", _stub)
    ttl = accounts.ENDPOINT_TTL_S

    await accounts.refresh_endpoint(NOW)  # poll fails → backoff armed
    assert state["calls"] == 1
    await accounts.refresh_endpoint(NOW + 1)  # dashboard render inside window
    await accounts.refresh_endpoint(NOW + ttl - 1)  # still inside window
    assert state["calls"] == 1  # no self-429 loop
    await accounts.refresh_endpoint(NOW + ttl + 1)  # window elapsed → one retry
    assert state["calls"] == 2
    state["status"] = 200  # account recovers
    await accounts.refresh_endpoint(NOW + 2 * ttl + 2)
    assert state["calls"] == 3
    assert str(cred) not in accounts._endpoint_backoff  # 200 cleared the backoff


async def test_refresh_endpoint_honors_failed_poll_retry_after(monkeypatch, tmp_path):
    """A failed usage poll with Retry-After backs off for the upstream window.

    Live 13/07 failure class: A/B returned ``429 Retry-After: 2397`` on
    ``/api/oauth/usage``, but the dashboard refresher retried after its fixed
    90 s TTL and re-created the same proxy 429/AIMD/advisor noise every cycle.
    """
    cred = tmp_path / "c.json"
    _write_cred(cred, "tok-x", expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "LISTEN_PORT", 8765)

    state = {"calls": 0}
    retry_after = 2397.0

    async def _stub(_url, _token):
        state["calls"] += 1
        return 429, None, retry_after

    monkeypatch.setattr(accounts, "_get_json", _stub)

    await accounts.refresh_endpoint(NOW)
    assert state["calls"] == 1
    await accounts.refresh_endpoint(NOW + accounts.ENDPOINT_TTL_S + 1)
    assert state["calls"] == 1
    await accounts.refresh_endpoint(NOW + retry_after - 1)
    assert state["calls"] == 1
    await accounts.refresh_endpoint(NOW + retry_after + 1)
    assert state["calls"] == 2


async def test_refresh_endpoint_polls_relaxed_cadence_when_retry_after_active(
    monkeypatch, tmp_path
):
    """A persisted bearer Retry-After relaxes (no longer gates) the usage poll.

    Pre-#103 the poller skipped entirely during a messages Retry-After window,
    starving the router of the one signal (usage decay) that can contradict a
    stale budget window for the full ~58 h it ran (13-14/07). The poller now
    fetches anyway — the usage endpoint is a separate rate-limit domain — at a
    cadence capped at ``_RETRY_AFTER_GATE_CAP_S`` so a long window is never
    hammered by UI auto-refresh. ``u7=100`` keeps the stale-clear guard dormant
    here; the clear path has its own focused test below.
    """
    token = "tok-" + "x"
    cred = tmp_path / "c.json"
    _write_cred(cred, token, expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "LISTEN_PORT", 8765)

    state = {"calls": 0}

    async def _stub(_url, _token):
        state["calls"] += 1
        return 200, _usage_body(u7=100.0)

    retry_after = 2397.0
    monkeypatch.setattr(accounts, "_get_json", _stub)
    monkeypatch.setattr(
        limiter,
        "_load_retry_after_state",
        lambda: {accounts._token_bearer_id(token): NOW + retry_after},
    )

    await accounts.refresh_endpoint(NOW)

    assert state["calls"] >= 1  # usage poll happened — a skip regression would be 0
    # Cadence capped at the gate, not the full 2397 s window.
    assert accounts._endpoint_backoff[str(cred)] == NOW + accounts._RETRY_AFTER_GATE_CAP_S
    cached = accounts._endpoint_cache[str(cred)]
    assert cached["err"] is None
    assert cached["usage"] is not None


async def test_maybe_clear_stale_retry_after_drops_long_window_on_usage_decay(
    monkeypatch, tmp_path
):
    """Fresh sub-exhaustion usage clears a long stale Retry-After window.

    A budget window noted at 100 % exhaustion ends at the reset epoch, but the
    rolling 5h/7d usage decays underneath. When the usage endpoint reports BOTH
    windows back below 1.0, the persisted window is dropped so the fleet
    re-adopts the account instead of concentrating on the others (13-14/07: two
    accounts blocked ~58 h). Uses real wall-clock time because ``clear_retry_after``
    re-derives ``now`` from ``time.time()`` internally.
    """
    token = "tok-" + "clear"
    bid = accounts._token_bearer_id(token)
    state_file = tmp_path / "retry-after.json"
    real_now = time.time()
    monkeypatch.setattr(config, "RETRY_AFTER_STATE_FILE", str(state_file))
    config.bearer_limiters.clear()

    # Long stale window + sub-exhaustion usage on BOTH windows -> cleared.
    state_file.write_text(json.dumps({bid: real_now + 5000}))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    accounts._maybe_clear_stale_retry_after(
        token, accounts._parse_usage(_usage_body(u5=40.0, u7=84.0)), real_now
    )
    assert bid not in json.loads(state_file.read_text())

    # Short window remaining (< 600 s) -> left alone: input-bucket rate pushback
    # that utilization figures say nothing about must expire on its own.
    state_file.write_text(json.dumps({bid: real_now + 60}))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    accounts._maybe_clear_stale_retry_after(
        token, accounts._parse_usage(_usage_body(u5=40.0, u7=84.0)), real_now
    )
    assert bid in json.loads(state_file.read_text())

    # Either window still exhausted (>= 1.0) -> left alone.
    state_file.write_text(json.dumps({bid: real_now + 5000}))
    monkeypatch.setattr(limiter, "_retry_after_state", None)
    accounts._maybe_clear_stale_retry_after(
        token, accounts._parse_usage(_usage_body(u5=40.0, u7=100.0)), real_now
    )
    assert bid in json.loads(state_file.read_text())


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


def test_parse_usage_scoped_prefers_active():
    # Codex MEDIUM: multiple weekly_scoped entries → pick the ACTIVE one.
    body = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "percent": 10,
                "is_active": False,
                "scope": {"model": {"display_name": "Fable"}},
            },
            {
                "kind": "weekly_scoped",
                "percent": 40,
                "is_active": True,
                "scope": {"model": {"display_name": "Sonnet"}},
            },
        ]
    }
    scoped = accounts._parse_usage(body)["scoped"]
    assert scoped["model"] == "Sonnet" and scoped["is_active"] is True
    # none active → deterministic fallback to the first
    body2 = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "percent": 10,
                "is_active": False,
                "scope": {"model": {"display_name": "Fable"}},
            }
        ]
    }
    assert accounts._parse_usage(body2)["scoped"]["model"] == "Fable"


def test_scoped_gauge_drops_stale_model_series_on_flip(monkeypatch):
    # Codex MEDIUM: when the scoped model flips Fable→Sonnet, the stale
    # per-model gauge series must be removed, not frozen.
    from anthropic_throttle_proxy import metrics as _m
    from anthropic_throttle_proxy.ui import routes as _routes

    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", "A:/x/c.json")
    _routes._scoped_model_seen.clear()
    ident = {"collapsed": False, "known": 0, "duplicates": {}}

    def sample(model):
        return _m.REGISTRY.get_sample_value(
            "anthropic_account_scoped_utilization", {"account": "A", "model": model}
        )

    _routes._publish_account_gauges(
        {"/x/c.json": {"usage": {"scoped": {"model": "Fable", "util": 0.1}}}}, ident
    )
    assert sample("Fable") == 0.1
    _routes._publish_account_gauges(
        {"/x/c.json": {"usage": {"scoped": {"model": "Sonnet", "util": 0.4}}}}, ident
    )
    assert sample("Sonnet") == 0.4
    assert sample("Fable") is None  # stale series removed on flip


# ── #128: persisted endpoint cache survives restart → unblanks the panel ────


def test_endpoint_cache_persists_and_reloads(tmp_path, monkeypatch):
    # Round-trip through disk, and PROVE no token/accessToken ever lands there.
    monkeypatch.setattr(config, "ENDPOINT_CACHE_FILE", tmp_path / "endpoint-cache.json")
    usage = accounts._parse_usage(_usage_body(u5=40.0, u7=84.0))
    accounts._endpoint_cache["/home/u/.claude/.credentials.json"] = {
        "fetched": NOW - 3600,
        "usage": usage,
        "err": None,
    }
    accounts._persist_endpoint_cache()

    text = (tmp_path / "endpoint-cache.json").read_text()
    assert "token" not in text and "accessToken" not in text  # invariant #2

    accounts._endpoint_cache.clear()
    accounts._load_endpoint_cache()
    entry = accounts._endpoint_cache["/home/u/.claude/.credentials.json"]
    assert entry["usage"]["util_7d"] == pytest.approx(0.84)
    assert entry["fetched"] == NOW - 3600
    assert "err" not in entry  # transient — dropped, not persisted


def test_load_drops_partial_usage_shape(tmp_path, monkeypatch):
    # GLM BLOCK: a hand-edited/corrupt persisted usage dict missing keys that
    # _fields_from_endpoint_usage subscripts must be DROPPED on load, so the
    # cache tier can never KeyError and 500 the /ui panel.
    monkeypatch.setattr(config, "ENDPOINT_CACHE_FILE", tmp_path / "endpoint-cache.json")
    (tmp_path / "endpoint-cache.json").write_text(
        '{"/home/u/.claude/.credentials.json": {"fetched": 1.0, "usage": {"util_5h": 0.4}}}'
    )
    accounts._endpoint_cache.clear()
    accounts._load_endpoint_cache()
    assert "/home/u/.claude/.credentials.json" not in accounts._endpoint_cache

    # A well-shaped full entry still round-trips (the guard is not over-broad).
    accounts._endpoint_cache.clear()
    accounts._endpoint_cache["/home/u/.claude/.credentials.json"] = {
        "fetched": NOW - 60,
        "usage": accounts._parse_usage(_usage_body(u5=40.0, u7=84.0)),
        "err": None,
    }
    accounts._persist_endpoint_cache()
    accounts._endpoint_cache.clear()
    accounts._load_endpoint_cache()
    assert "/home/u/.claude/.credentials.json" in accounts._endpoint_cache


async def test_cold_restart_429_keeps_persisted_entry(tmp_path, monkeypatch):
    # An aged 200 entry seeded from disk must be RETAINED through a cold 429
    # poll (stale-keep branch), not overwritten by a cold-blank.
    cred = tmp_path / "c.json"
    _write_cred(cred, "tok-x", expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "LISTEN_PORT", 8765)
    monkeypatch.setattr(config, "ENDPOINT_CACHE_FILE", tmp_path / "endpoint-cache.json")

    # Seed disk with an aged reading, then reload (simulating the restart seed).
    aged = accounts._parse_usage(_usage_body(u5=40.0, u7=84.0))
    (tmp_path / "endpoint-cache.json").write_text(
        json.dumps({str(cred): {"fetched": NOW - 3 * 3600, "usage": aged}})
    )

    async def _stub_429(_url, _token):
        return 429, None, None

    monkeypatch.setattr(accounts, "_get_json", _stub_429)
    # fetched aged past the TTL so the poll actually fires and 429s.
    await accounts.refresh_endpoint(NOW)

    entry = accounts._endpoint_cache[str(cred)]
    assert entry["usage"] is not None  # stale-keep branch, NOT cold-blank
    assert entry["usage"]["util_7d"] == pytest.approx(0.84)
    assert "unavailable" in entry["err"]  # failure marked


def test_account_view_cache_tier_unblanks(tmp_path, monkeypatch):
    # Aged persisted usage, no proxy-unified, 7d open (future reset), 5h rolled
    # over (past reset) → cache tier renders real aged 7d + stale 5h.
    cred = tmp_path / "a.json"
    _write_cred(cred, "tok-a", expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    accounts._endpoint_cache[str(cred)] = {
        "fetched": NOW - 3 * 3600,  # ~3h old
        "usage": accounts._parse_usage(
            _usage_body(u5=95.0, u7=84.0, r5=NOW - 100, r7=NOW + 2 * DAY)
        ),
        "err": None,
    }

    # No bearers → no proxy-unified fallback; endpoint arg is None (not fresh).
    (view,) = accounts.account_view([], NOW, None)
    assert view["src"] == "cache"
    assert view["cache_age"]  # non-empty age string
    assert view["win7"]["pct"] == 84 and view["win7"]["stale"] is False
    assert view["win7"]["reset_in"]  # real aged reset shown
    assert view["win5"]["stale"] is True and view["win5"]["pct"] == 0  # rolled over


def test_locked_note_from_uncapped_last_ratelimit(tmp_path, monkeypatch):
    # A never-read, budget-locked account with usage NOWHERE must still show a
    # locked marker sourced from the bearer's UNCAPPED live Retry-After — not
    # the 900s-capped persisted state (which expires ~15min post-restart).
    token = "tok-locked"  # noqa: S105 — test fixture, not a real credential
    cred = tmp_path / "a.json"
    _write_cred(cred, token, expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")

    bid = _expected_bid(token)
    bearers = [{"bearer_id": bid, "last_ratelimit": {"retry-after": "201223"}}]
    (view,) = accounts.account_view(bearers, NOW)

    assert view["src"] == "none"  # no usage reading anywhere
    assert view["win7"] is None
    assert view["locked_in"]  # non-empty duration string, not a percentage
    assert "%" not in view["locked_in"]
    assert view["locked_in"] == accounts._fmt_duration(201223)  # 2d 07h


async def test_401_tombstones_disk_entry(tmp_path, monkeypatch):
    # A dead credential (401) must REMOVE the on-disk entry so it cannot re-seed
    # aged numbers after a restart.
    cred = tmp_path / "a.json"
    _write_cred(cred, "tok-dead", expires_at_ms=int((NOW + 3600) * 1000))
    monkeypatch.setattr(config, "ACCOUNT_CRED_PATHS", f"A:{cred}")
    monkeypatch.setattr(config, "LISTEN_PORT", 8765)
    cache_file = tmp_path / "endpoint-cache.json"
    monkeypatch.setattr(config, "ENDPOINT_CACHE_FILE", cache_file)

    # Seed disk with a healthy 200 entry first.
    cache_file.write_text(
        json.dumps({str(cred): {"fetched": NOW, "usage": accounts._parse_usage(_usage_body())}})
    )

    async def _stub_401(_url, _token):
        return 401, None, None

    monkeypatch.setattr(accounts, "_get_json", _stub_401)
    await accounts.refresh_endpoint(NOW + accounts.ENDPOINT_TTL_S + 1)

    assert str(cred) not in json.loads(cache_file.read_text())  # tombstoned on disk
