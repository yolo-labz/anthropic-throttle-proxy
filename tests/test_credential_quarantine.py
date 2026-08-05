"""Per-bearer credential quarantine.

The incident this covers, measured on the desktop fleet 04/08/2026: one of three
configured accounts answered every ``POST /v1/messages`` with

    403 {"type":"error","error":{"type":"permission_error","message":"OAuth
     authentication is currently not allowed for this organization.",
     "details":{"error_code":"oauth_not_allowed_for_organization"}}}

A 403 is neither AIMD pushback nor a budget window, so nothing gated the bearer:
it carried no Retry-After and no unified gauges, which made it read as the
idlest, cheapest account in the fleet. The half-open retry probe elected it 41
times in three hours, resolved ``closed`` 41 times, and every election spent a
REAL client turn — 40 sessions were handed the 403 instead of an answer.
"""

from __future__ import annotations

import gzip
import json
import math
import zlib

import pytest

from anthropic_throttle_proxy import config, limiter, proxy

# The verbatim upstream envelope (request_id elided).
ORG_DISABLED_BODY = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "permission_error",
            "message": "OAuth authentication is currently not allowed for this organization.",
            "details": {"error_code": "oauth_not_allowed_for_organization"},
        },
    }
).encode()

# Same status and same error.type, but a per-REQUEST permission failure. Must NOT
# quarantine: taking a healthy account out of rotation over one unusable model
# would be a worse outage than the one this feature fixes.
MODEL_DENIED_BODY = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "permission_error",
            "message": "This account does not have access to the requested model.",
        },
    }
).encode()


@pytest.fixture(autouse=True)
def _isolate_bearer_state():
    config.bearer_state.clear()
    yield
    config.bearer_state.clear()


def _bstate(bid: str) -> dict:
    return config.bearer_state.setdefault(bid, {})


# ── classification ──────────────────────────────────────────────────────────


def test_org_disabled_403_is_credential_death():
    assert (
        proxy._credential_dead_reason(403, bytearray(ORG_DISABLED_BODY))
        == "oauth_not_allowed_for_organization"
    )


def test_401_is_credential_death_on_status_alone():
    assert proxy._credential_dead_reason(401, None) == "status-401"


@pytest.mark.parametrize(
    "status,body",
    [
        (403, bytearray(MODEL_DENIED_BODY)),  # per-request permission, not the credential
        (403, None),  # empty envelope — the streak floor owns this one
        (403, bytearray(b"not json at all")),
        (429, bytearray(ORG_DISABLED_BODY)),  # rate limit is not credential death
        (200, bytearray(b"{}")),
    ],
)
def test_unrecognised_responses_do_not_classify_as_dead(status, body):
    assert proxy._credential_dead_reason(status, body) == ""


def test_extra_codes_are_honoured_without_a_release(monkeypatch):
    body = bytearray(
        json.dumps(
            {"error": {"type": "permission_error", "details": {"error_code": "some_new_code"}}}
        ).encode()
    )
    assert proxy._credential_dead_reason(403, body) == ""
    monkeypatch.setattr(config, "CREDENTIAL_DEAD_CODES_EXTRA", "some_new_code, another")
    assert proxy._credential_dead_reason(403, body) == "some_new_code"


# ── quarantine bookkeeping ──────────────────────────────────────────────────


def test_recognised_403_quarantines_on_the_first_response():
    bstate = _bstate("dead01")
    proxy._note_bearer_credential("dead01", bstate, 403, bytearray(ORG_DISABLED_BODY))
    assert proxy._bearer_credential_dead("dead01")
    assert bstate["credential"]["reason"] == "oauth_not_allowed_for_organization"
    assert bstate["credential"]["status"] == 403


def test_unrecognised_rejections_quarantine_only_after_the_streak(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_DEAD_STREAK", 3)
    bstate = _bstate("dead02")
    for _ in range(2):
        proxy._note_bearer_credential("dead02", bstate, 403, None)
        assert not proxy._bearer_credential_dead("dead02")
    proxy._note_bearer_credential("dead02", bstate, 403, None)
    assert proxy._bearer_credential_dead("dead02")
    assert bstate["credential"]["reason"] == "streak-3"


def test_a_success_resets_the_streak(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_DEAD_STREAK", 3)
    bstate = _bstate("live01")
    proxy._note_bearer_credential("live01", bstate, 403, None)
    proxy._note_bearer_credential("live01", bstate, 403, None)
    proxy._note_bearer_credential("live01", bstate, 200, bytearray(b"{}"))
    assert bstate.get("credential_streak") is None
    proxy._note_bearer_credential("live01", bstate, 403, None)
    assert not proxy._bearer_credential_dead("live01")


def test_a_success_clears_an_existing_quarantine():
    bstate = _bstate("dead03")
    proxy._note_bearer_credential("dead03", bstate, 403, bytearray(ORG_DISABLED_BODY))
    assert proxy._bearer_credential_dead("dead03")
    proxy._note_bearer_credential("dead03", bstate, 200, bytearray(b"{}"))
    assert not proxy._bearer_credential_dead("dead03")
    assert "credential" not in bstate


def test_quarantine_disarms_the_half_open_retry_gate():
    """The 41-elections loop: only a success disarms a gate, and a refused
    credential has none to give, so the gate must be dropped explicitly."""
    limiter.require_retry_probe("dead04", block_while_retry=True)
    assert limiter.retry_probe_required("dead04")
    proxy._note_bearer_credential("dead04", _bstate("dead04"), 403, bytearray(ORG_DISABLED_BODY))
    assert not limiter.retry_probe_required("dead04")


# ── routing ─────────────────────────────────────────────────────────────────


def test_dead_account_is_never_a_routing_candidate():
    """Even with a probe explicitly allowed — that election IS the bug."""
    acct = {"bearer_id": "dead05", "label": "A"}
    assert proxy._account_routing_candidate_score(acct, "other") < math.inf
    proxy._note_bearer_credential("dead05", _bstate("dead05"), 403, bytearray(ORG_DISABLED_BODY))
    assert proxy._account_routing_candidate_score(acct, "other") == math.inf
    assert proxy._account_routing_candidate_score(acct, "other", allow_retry_probe=True) == math.inf
    assert (
        proxy._account_routing_candidate_score(
            acct,
            "dead05",
            allow_retry_probe=True,
            allow_pressure=True,
            allow_target_spillover=True,
        )
        == math.inf
    )


def test_a_healthy_sibling_still_scores_finite():
    proxy._note_bearer_credential("dead06", _bstate("dead06"), 403, bytearray(ORG_DISABLED_BODY))
    healthy = {"bearer_id": "live02", "label": "B"}
    assert proxy._account_routing_candidate_score(healthy, "dead06") < math.inf


# ── recovery ────────────────────────────────────────────────────────────────


async def test_recheck_reopens_only_on_a_real_message_body(monkeypatch):
    bstate = _bstate("dead07")
    proxy._note_bearer_credential("dead07", bstate, 403, bytearray(ORG_DISABLED_BODY))

    calls: list[str] = []

    async def fake_recheck(bid: str, token: str) -> None:
        calls.append(bid)

    monkeypatch.setattr(config, "CREDENTIAL_RECHECK_S", 900.0)
    monkeypatch.setattr(config, "CREDENTIAL_RECHECK_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(proxy, "_credential_recheck_one", fake_recheck)

    from anthropic_throttle_proxy import accounts

    monkeypatch.setattr(
        accounts,
        "routing_snapshot",
        lambda now=None: [
            {"bearer_id": "dead07", "token": "tok-dead", "label": "A"},
            {"bearer_id": "live03", "token": "tok-live", "label": "B"},
        ],
    )

    # last_checked was stamped at quarantine time, so nothing is due yet.
    await proxy._credential_recheck_once()
    assert calls == []

    bstate["credential"]["last_checked"] = 0.0
    await proxy._credential_recheck_once()
    assert calls == ["dead07"], "only the quarantined account is re-probed"


async def test_recheck_is_disabled_when_the_interval_is_zero(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_RECHECK_S", 0.0)
    called = False

    def boom(*_a, **_k):  # pragma: no cover - must never run
        nonlocal called
        called = True
        return []

    from anthropic_throttle_proxy import accounts

    monkeypatch.setattr(accounts, "routing_snapshot", boom)
    await proxy._credential_recheck_once()
    assert not called


# ── compressed error envelopes ──────────────────────────────────────────────
#
# `_forward_once` captures an error body EXACTLY as it came off the wire and
# Anthropic serves it gzipped, so every JSON consumer of `captured` was parsing
# compressed bytes. Measured 04/08/2026 against the live 403: the body began
# `1f 8b`, classification fell through, and quarantine cost 3 turns via the
# streak floor instead of 1. Same root cause as the `reason=empty_body` the 413
# diagnostic (PR #19/#20) kept reporting for non-empty bodies.


def test_gzipped_403_still_classifies():
    body = bytearray(gzip.compress(ORG_DISABLED_BODY))
    assert body[:2] == b"\x1f\x8b", "fixture must actually be gzip"
    assert proxy._credential_dead_reason(403, body) == "oauth_not_allowed_for_organization", (
        "a gzipped envelope must classify identically to a plain one"
    )


def test_gzipped_403_quarantines_on_the_first_response():
    bstate = _bstate("dead08")
    proxy._note_bearer_credential(
        "dead08", bstate, 403, bytearray(gzip.compress(ORG_DISABLED_BODY))
    )
    assert proxy._bearer_credential_dead("dead08")
    assert bstate["credential"]["reason"] == "oauth_not_allowed_for_organization"
    assert "organization" in bstate["credential"]["detail"], "detail must survive decompression"


def test_deflate_and_plain_bodies_both_parse():
    assert proxy._error_envelope_fields(bytearray(zlib.compress(ORG_DISABLED_BODY)))[0] == (
        "permission_error"
    )
    assert proxy._error_envelope_fields(bytearray(ORG_DISABLED_BODY))[0] == "permission_error"


def test_decompressed_body_never_raises_on_garbage():
    """Unknown/corrupt input is returned unchanged so callers can always parse."""
    assert proxy.decompressed_body(b"") == b""
    assert proxy.decompressed_body(b"{}") == b"{}"
    assert proxy.decompressed_body(b"\x1f\x8btruncated") == b"\x1f\x8btruncated"
    assert proxy.decompressed_body(b"\x78garbage") == b"\x78garbage"


# ── persistence across restarts ──────────────────────────────────────────────
#
# A quarantine held only in memory is re-earned on every restart, at the cost of
# real client turns — and the proxy restarts on every system activation
# (measured 04/08/2026: 13 restarts in one day on the desktop). Unlike a
# Retry-After window, a restored credential verdict is NOT capped: a refused
# credential does not decay the way a rolling budget does, and the synthetic
# re-check — which costs no client turn — is what un-sticks it.


@pytest.fixture
def cred_state(tmp_path, monkeypatch):
    path = tmp_path / "credential-state.json"
    monkeypatch.setattr(config, "CREDENTIAL_STATE_FILE", str(path))
    proxy._restored_credentials.clear()
    yield path
    proxy._restored_credentials.clear()


def test_quarantine_is_written_to_disk(cred_state):
    proxy._note_bearer_credential("deadP1", _bstate("deadP1"), 403, bytearray(ORG_DISABLED_BODY))
    on_disk = json.loads(cred_state.read_text())
    assert on_disk["deadP1"]["ok"] is False
    assert on_disk["deadP1"]["reason"] == "oauth_not_allowed_for_organization"


def test_restart_restores_the_quarantine_without_a_single_dead_turn(cred_state):
    proxy._note_bearer_credential("deadP2", _bstate("deadP2"), 403, bytearray(ORG_DISABLED_BODY))
    # Simulate a restart: process-global state is lost, the file is not.
    config.bearer_state.clear()
    proxy._restored_credentials.clear()
    assert not proxy._bearer_credential_dead("deadP2"), "precondition: memory really was cleared"
    proxy.load_credential_state()
    assert proxy._bearer_credential_dead("deadP2")
    acct = {"bearer_id": "deadP2", "label": "A"}
    assert proxy._account_routing_candidate_score(acct, "x", allow_retry_probe=True) == math.inf


def test_recovery_removes_it_from_disk(cred_state):
    bstate = _bstate("deadP3")
    proxy._note_bearer_credential("deadP3", bstate, 403, bytearray(ORG_DISABLED_BODY))
    assert json.loads(cred_state.read_text())
    proxy._note_bearer_credential("deadP3", bstate, 200, bytearray(b"{}"))
    assert json.loads(cred_state.read_text()) == {}, "a recovered account must not be resurrected"


def test_a_live_verdict_beats_a_restored_one(cred_state):
    """An in-process 2xx must win over stale disk state."""
    proxy._restored_credentials["deadP4"] = {"ok": False, "status": 403, "reason": "stale"}
    bstate = _bstate("deadP4")
    proxy._note_bearer_credential("deadP4", bstate, 200, bytearray(b"{}"))
    assert not proxy._bearer_credential_dead("deadP4")


def test_no_state_file_configured_is_a_no_op(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_STATE_FILE", "")
    proxy._restored_credentials.clear()
    proxy._note_bearer_credential("deadP5", _bstate("deadP5"), 403, bytearray(ORG_DISABLED_BODY))
    assert proxy._bearer_credential_dead("deadP5"), "quarantine still works without persistence"
    proxy.load_credential_state()  # must not raise


def test_corrupt_state_file_is_a_clean_start(cred_state):
    cred_state.write_text("{not json")
    proxy.load_credential_state()
    assert proxy._restored_credentials == {}


# ── the restored verdict must stay VISIBLE ───────────────────────────────────
#
# Routing reads `_restored_credentials`, but /__throttle/health rendered only
# `bearer_state` — so after a restart the `credential` row vanished from the
# payload while the bearer stayed correctly quarantined. Not cosmetic:
# `claude-account-pick` gates its own account choice on exactly this field
# (NixOS #1634), so a restart left the LAUNCH picker electing the dead account
# again while the proxy itself refused it. Measured on the desktop 05/08/2026:
# `.bearers.<bid>.credential.ok` read `null` and the picker returned `pick=a`.


async def _health_bearers() -> dict:
    resp = await proxy.health(None)
    return json.loads(resp.body).get("bearers") or {}


async def test_health_shows_a_restored_quarantine(cred_state):
    proxy._note_bearer_credential("deadH1", _bstate("deadH1"), 403, bytearray(ORG_DISABLED_BODY))
    # Simulate the restart: process-global state gone, disk intact.
    config.bearer_state.clear()
    proxy._restored_credentials.clear()
    proxy.load_credential_state()
    assert proxy._bearer_credential_dead("deadH1"), "precondition: routing still gates it"

    bearers = await _health_bearers()
    assert "deadH1" in bearers, "a restored quarantine must appear in health at all"
    assert bearers["deadH1"]["credential"]["ok"] is False
    assert bearers["deadH1"]["credential"]["reason"] == "oauth_not_allowed_for_organization"


async def test_the_restored_copy_never_masks_a_live_row(cred_state):
    """`setdefault`, not assignment: a bearer holding a LIVE verdict keeps it.

    Only asserts what the merge actually promises. A bearer already marked dead
    by the restored map short-circuits `_quarantine_bearer` (it does not re-log
    or rewrite), so this deliberately does NOT claim a live 403 refreshes a
    stale stored `reason` — that is not a behaviour the design offers.
    """
    bstate = _bstate("deadH2")
    bstate["clients"] = {}
    proxy._note_bearer_credential("deadH2", bstate, 403, bytearray(ORG_DISABLED_BODY))
    # A disk copy that disagrees must not overwrite the live row in the view.
    proxy._restored_credentials["deadH2"] = {"ok": False, "reason": "stale-from-disk"}
    bearers = await _health_bearers()
    assert bearers["deadH2"]["credential"]["reason"] == "oauth_not_allowed_for_organization"
