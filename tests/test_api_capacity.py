"""api-capacity.v1: a projection of the existing apiCredits receipt plus header lanes.

Semantic controls: a stale receipt shows no amount as current, an insufficient-credit
receipt is ``remaining: null`` (never 0), USD and quota percent are never totalled,
and no raw account/workspace/bearer id reaches JSON, HTML or logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import aiohttp_jinja2
import jinja2
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import api_capacity as ac

try:  # PR #28's token gate; until it lands api_capacity fails closed on its own.
    from anthropic_throttle_proxy.ui import auth
except ImportError:  # pragma: no cover - the fallback is what main ships
    auth = None

needs_gate = pytest.mark.skipif(auth is None, reason="ui.auth token gate (PR #28) not merged")

RECEIPT = datetime.fromisoformat("2026-11-08T18:05:00-03:00").timestamp()
RAW_ACCOUNT = "acct-SYNTHETIC-001"
RAW_WORKSPACE = "wrkspc-synthetic-002"
BID = "a1b2c3d4"
LEAKY = "-".join(("sk", "ant", "api03", "synthetic0do0not0use"))


def _receipt(**credit) -> dict:
    return {
        "apiCredits": {
            "schema": 1,
            "accounts": [
                {
                    "id": "anthropic-api",
                    "provider": "anthropic",
                    "accountId": RAW_ACCOUNT,
                    "workspaceId": RAW_WORKSPACE,
                    "currency": "USD",
                    "source": "operator-receipt",
                    "observedAt": credit.pop("observed", "2026-11-08T18:05:00-03:00"),
                    "validUntil": credit.pop("valid", "2026-11-08T19:05:00-03:00"),
                    "authorized": True,
                    "credit": {"status": "available", "remaining": 42.5, **credit},
                    "rateLimits": [
                        {
                            "model": "claude-x",
                            "scope": "default",
                            "rpmRemaining": 40,
                            "tpmRemaining": 30000,
                        }
                    ],
                    "spendLimit": {"remaining": 100.0, "enforced": True},
                }
            ],
        }
    }


def _snap(tmp_path, receipt, *, now, bearer_state=None, candidate=None):
    path = tmp_path / "api-credits.json"
    path.write_text(json.dumps(receipt))
    return ac.snapshot(
        credits=ac.read_credits(str(path), now),
        bearer_state=bearer_state or {},
        candidate_bid=candidate,
        api_label="Anthropic API",
        now=now,
    )


def _account(snap) -> dict:
    assert len(snap["accounts"]) == 1
    return snap["accounts"][0]


def test_fresh_receipt_keeps_every_field_and_labels_each_one(tmp_path):
    acct = _account(_snap(tmp_path, _receipt(), now=RECEIPT + 60))
    credit, rates, spend = (acct["meters"][k] for k in ("credit", "rate_limits", "spend_limit"))
    assert (credit["status"], credit["label"], credit["remaining"], credit["currency"]) == (
        "available",
        "fresh",
        42.5,
        "USD",
    )
    assert credit["display"] == "USD 42.50"
    assert credit["observed_at"] == "2026-11-08T18:05:00-03:00"
    assert acct["valid_until"] == "2026-11-08T19:05:00-03:00"
    assert acct["source"] == "operator-receipt" and acct["id"] == "anthropic-api"
    assert rates == [
        {
            "model": "claude-x",
            "scope": "default",
            "rpm_remaining": 40.0,
            "rpm_label": "fresh",
            "tpm_remaining": 30000.0,
            "tpm_label": "fresh",
            **{k: acct[k] for k in ("observed_at", "valid_until", "age_s", "fresh", "freshness")},
        }
    ]
    assert (spend["remaining"], spend["currency"], spend["enforced"]) == (100.0, "USD", True)
    assert (spend["remaining_label"], spend["enforced_label"]) == ("fresh", "fresh")


def test_stale_receipt_shows_no_amount_as_current_even_when_rerendered(tmp_path):
    later = RECEIPT + 5 * 3600  # past validUntil; re-reading never refreshes it
    for _ in range(2):
        acct = _account(_snap(tmp_path, _receipt(), now=later))
        credit = acct["meters"]["credit"]
        assert (credit["status"], credit["label"], credit["remaining"]) == ("stale", "stale", None)
        assert credit["display"] == "balance unknown · stale"
        assert credit["observed_at"] == "2026-11-08T18:05:00-03:00"
        assert (acct["fresh"], acct["freshness"]) == (False, "stale")
        rate = acct["meters"]["rate_limits"][0]
        assert (rate["rpm_remaining"], rate["rpm_label"]) == (None, "stale")
        spend = acct["meters"]["spend_limit"]
        assert (spend["remaining"], spend["remaining_label"], spend["enforced_label"]) == (
            None,
            "stale",
            "stale",
        )
        assert "42.5" not in ac.render_fragment({**_snap(tmp_path, _receipt(), now=later)})


def test_future_observation_is_never_fresh(tmp_path):
    receipt = _receipt(observed="2026-11-08T23:59:00-03:00", valid="2026-11-09T23:59:00-03:00")
    acct = _account(_snap(tmp_path, receipt, now=RECEIPT))
    assert (acct["fresh"], acct["freshness"]) == (False, "future")
    assert acct["meters"]["credit"]["remaining"] is None


def test_insufficient_credit_receipt_is_balance_unknown_never_zero(tmp_path):
    snap = _snap(tmp_path, _receipt(status="refused", remaining=0), now=RECEIPT + 60)
    credit = _account(snap)["meters"]["credit"]
    assert (credit["status"], credit["remaining"], credit["currency"]) == ("refused", None, None)
    assert credit["display"] == "balance unknown"
    fragment = ac.render_fragment(snap)
    assert "balance unknown" in fragment and 'class="crit"' in fragment
    assert "USD 0.00" not in fragment
    assert snap["apiCredits"]["accounts"][0]["credit"]["remaining"] is None


def test_pending_credit_is_labelled_pending_without_amount(tmp_path):
    credit = _account(_snap(tmp_path, _receipt(status="pending"), now=RECEIPT + 60))["meters"][
        "credit"
    ]
    assert (credit["label"], credit["remaining"]) == ("pending", None)


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_usd_and_quota_percent_are_never_totalled(tmp_path):
    snap = _snap(tmp_path, _receipt(), now=RECEIPT + 60)
    assert not any("total" in key or "util" in key for key in _walk_keys(snap))
    assert set(snap["subscription"]) == {
        "state",
        "href",
        "pointer",
        "observed_at",
        "age_s",
        "fresh",
        "freshness",
    }
    assert snap["subscription"]["href"] == "/__throttle/admission"
    assert "%" not in ac.render_fragment(snap)


@pytest.mark.parametrize(
    ("setup", "state"),
    [(None, "unconfigured"), ("missing", "absent"), ("big", "too_large"), ("junk", "invalid")],
)
def test_unavailable_receipt_yields_no_accounts(tmp_path, setup, state):
    path = ""
    if setup == "missing":
        path = str(tmp_path / "nope.json")
    elif setup == "big":
        path = str(tmp_path / "big.json")
        Path(path).write_text(" " * (ac.MAX_CREDITS_BYTES + 1))
    elif setup == "junk":
        path = str(tmp_path / "junk.json")
        Path(path).write_text("{not json")
    got_state, normalized = ac.read_credits(path, RECEIPT)
    assert (got_state, normalized["accounts"]) == (state, [])


def _meta(reset: str) -> dict[str, str]:
    return {
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "49",
        "anthropic-ratelimit-requests-reset": reset,
        "anthropic-ratelimit-tokens-limit": "80000",
        "anthropic-ratelimit-tokens-remaining": "79000",
        "anthropic-ratelimit-tokens-reset": reset,
    }


def test_no_raw_id_reaches_json_html_or_logs(tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    receipt = _receipt()
    receipt["apiCredits"]["accounts"][0]["id"] = LEAKY  # an id shaped like a key
    snap = _snap(
        tmp_path,
        receipt,
        now=RECEIPT + 60,
        bearer_state={BID: {"last_ratelimit": _meta(""), "last_ratelimit_at": RECEIPT}},
        candidate=BID,
    )
    acct = _account(snap)
    assert acct["account_ref"] == "h:" + hashlib.sha256(RAW_ACCOUNT.encode()).hexdigest()[:8]
    assert snap["apiCredits"]["accounts"][0]["accountId"] == acct["account_ref"]
    blobs = (json.dumps(snap), ac.render_fragment(snap), caplog.text, *capsys.readouterr())
    for blob in blobs:
        for raw in (RAW_ACCOUNT, RAW_WORKSPACE, BID, LEAKY):
            assert raw not in blob


def test_header_rate_limit_freshness_comes_from_observation_time_only(tmp_path):
    reset = "2026-11-08T21:06:00Z"
    bearer_state = {
        BID: {"last_ratelimit": _meta(reset), "last_ratelimit_at": RECEIPT},
        "oauthbid": {"last_ratelimit": {"anthropic-ratelimit-unified-status": "allowed"}},
    }
    snap = _snap(tmp_path, _receipt(), now=RECEIPT + 30, bearer_state=bearer_state, candidate=BID)
    assert [lane["lane"] for lane in snap["header_lanes"]] == ["api:anthropic-api"]
    rate = snap["header_lanes"][0]["rate_limit"]
    assert (rate["fresh"], rate["values"]["requests_remaining"]) == (True, 49)
    assert rate["observed_at"] == "2026-11-08T21:05:00Z"
    later = _snap(
        tmp_path, _receipt(), now=RECEIPT + 3600, bearer_state=bearer_state, candidate=BID
    )
    meter = later["header_lanes"][0]["rate_limit"]
    assert (meter["fresh"], meter["freshness"], meter["reset_passed"]) == (False, "stale", True)


def test_header_lanes_without_or_with_future_observation_are_never_fresh(tmp_path):
    bearer_state = {
        BID: {"last_ratelimit": _meta("2026-11-08T23:00:00Z")},
        "api-key": {"last_ratelimit": _meta(""), "last_ratelimit_at": RECEIPT + 600},
    }
    lanes = {
        lane["lane"]: lane["rate_limit"]
        for lane in _snap(tmp_path, _receipt(), now=RECEIPT, bearer_state=bearer_state)[
            "header_lanes"
        ]
    }
    assert (lanes["api:observed-1"]["fresh"], lanes["api:observed-1"]["observed_at"]) == (
        False,
        None,
    )
    assert (lanes["api:caller"]["fresh"], lanes["api:caller"]["freshness"]) == (False, "future")


async def test_routes_serve_json_and_panel(tmp_path, monkeypatch):
    path = tmp_path / "api-credits.json"
    path.write_text(json.dumps(_receipt(status="refused", remaining=0)))
    monkeypatch.setattr(
        ac,
        "_live_inputs",
        lambda: {
            "credits_path": str(path),
            "bearer_state": {},
            "candidate_bid": None,
            "api_label": "Anthropic API",
        },
    )
    app = web.Application()
    ac.attach_api_capacity(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/__throttle/api-capacity")
        assert resp.status == 200
        doc = await resp.json()
        page = await (await client.get("/ui/api-capacity")).text()
        fragment = await (await client.get("/ui/api-capacity", headers={"HX-Request": "1"})).text()
    assert doc["schema"] == "api-capacity.v1" and doc["credits_file"]["state"] == "ok"
    assert "htmx.org@1.9.12" in page and "<html" not in fragment
    assert "balance unknown" in fragment
    assert RAW_ACCOUNT not in page + fragment + json.dumps(doc)
    assert "#" not in fragment  # token classes only, no raw hex colours


# A relayed request counts as remote under ui.auth (no middleware needed).
RELAYED = {"X-Forwarded-For": "203.0.113.7", "HX-Request": "true"}


@pytest.fixture
def gated_app(tmp_path, monkeypatch):
    token = tmp_path / "ui-token"
    token.write_text("fixture-ui-token-9c1e\n")
    if auth is not None:
        monkeypatch.setenv(auth.ENV, str(token))
    credits = tmp_path / "api-credits.json"
    credits.write_text(json.dumps(_receipt()))
    monkeypatch.setattr(
        ac,
        "_live_inputs",
        lambda: {
            "credits_path": str(credits),
            "bearer_state": {},
            "candidate_bid": None,
            "api_label": "Anthropic API",
        },
    )
    if auth is not None:
        auth._load.cache_clear()

    app = web.Application()
    ac.attach_api_capacity(app)
    yield app
    if auth is not None:
        auth._load.cache_clear()


async def test_relayed_peer_is_refused_until_a_token_gate_exists(gated_app):
    """Whatever owns remote access, a relayed peer never reads the receipt.

    With PR #28 the refusal is 401; without it api_capacity rejects the peer itself
    (403, ``loopback-only``). Both are fails closed — the panel never publishes
    credits to an unauthenticated remote caller, and the body carries no receipt.
    """
    async with TestClient(TestServer(gated_app)) as client:
        for path in ("/__throttle/api-capacity", "/ui/api-capacity"):
            r = await client.get(path, headers=RELAYED)
            assert r.status in (401, 403), path
            body = await r.text()
            assert RAW_ACCOUNT not in body
            assert "sk-ant" not in body
        if auth is None:
            return
        ok = await client.get(
            "/__throttle/api-capacity",
            headers={**RELAYED, auth.AUTH_HEADER: "fixture-ui-token-9c1e"},
        )
        assert ok.status == 200


async def test_loopback_peer_is_allowed_and_reports_its_gate(gated_app):
    async with TestClient(TestServer(gated_app)) as client:
        doc = await (await client.get("/__throttle/api-capacity")).json()
        fragment = await client.get("/ui/api-capacity", headers={"HX-Request": "true"})
        assert fragment.status == 200
    assert doc["remote_access_gate"] == ac.gate_state()
    assert doc["remote_access_gate"] in {"ui-auth", "none", "loopback-only"}


def test_gate_state_never_claims_open_when_it_is_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("THROTTLE_UI_TOKEN_FILE", raising=False)
    if auth is None:
        # No gate module: remote peers are refused by this module, never "none".
        assert ac.gate_state() == "loopback-only"
        return
    auth._load.cache_clear()
    assert ac.gate_state() == "none"
    monkeypatch.setenv(auth.ENV, str(tmp_path / "missing-token"))
    auth._load.cache_clear()
    assert ac.gate_state() == "ui-auth"
    auth._load.cache_clear()


def test_dashboard_card_is_env_gated_and_rendered_without_javascript(monkeypatch):
    """The card exists only when a receipt is configured, and never needs a module."""
    monkeypatch.delenv("THROTTLE_API_CREDITS_FILE", raising=False)
    assert ac.panel_enabled() is False
    monkeypatch.setenv("THROTTLE_API_CREDITS_FILE", "/var/lib/throttle/api-credits.json")
    assert ac.panel_enabled() is True
    templates = Path(__file__).resolve().parents[1] / "src/anthropic_throttle_proxy/ui/templates"
    page = (templates / "dashboard.html").read_text()
    assert 'id="api-capacity"' in page
    assert 'hx-get="/ui/api-capacity"' in page
    assert "api_capacity_enabled()" in page
    assert page.count("<script") == 1  # the HTMX runtime, never a module


async def test_globals_helper_publishes_the_flag_for_the_template():
    app = web.Application()
    aiohttp_jinja2.setup(app, loader=jinja2.DictLoader({"x.html": "{{ api_capacity_enabled() }}"}))
    ac.attach_api_capacity_globals(app)
    assert aiohttp_jinja2.get_env(app).globals["api_capacity_enabled"] is ac.panel_enabled
