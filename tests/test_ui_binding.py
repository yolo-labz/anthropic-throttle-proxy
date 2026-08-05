"""The binding constraint is an object, not a clause in a sentence.

Mid-incident the operator needs three facts: which paid subscription is
blocked, when its window reopens, and what takes traffic meanwhile. The status
strip used to carry only `binding: 7d window 100% on b144f62f` — a hash, no
row, no way out (cross-family design review, 05/08/2026, ranked P0).
"""

from __future__ import annotations

from anthropic_throttle_proxy.ui import routes


def _sub(label, bearer, pct, status="ok", window="7d"):
    return {
        "id": label,
        "sub": f"{label.lower()}@example.test",
        "bearer_id": bearer,
        "family": "anthropic",
        "status": status,
        "meters": [{"label": window, "pct": pct, "reset_in": "2d 3h"}],
    }


def test_binding_resolves_to_a_named_subscription_and_a_way_out():
    status = {"binding": {"bearer_id": "b144f62f", "window": "7d", "pct": 100, "retry_after": None}}
    subs = [_sub("A", "b144f62f", 100.0), _sub("B", "47f0b262", 21.0), _sub("C", "666a53af", 55.0)]

    routes._attach_binding(status, subs)

    assert status["binding"]["subscription"] == "A"
    assert status["binding"]["resets_in"] == "2d 3h"
    # The freest SERVING sibling, not simply the next row.
    assert status["binding"]["next_usable"] == "B"
    assert status["binding"]["next_usable_pct"] == 21
    assert subs[0]["is_binding"] is True
    assert "is_binding" not in subs[1]


def test_no_usable_sibling_is_stated_not_left_blank():
    status = {"binding": {"bearer_id": "b1", "window": "5h", "pct": 100, "retry_after": 900}}
    subs = [_sub("A", "b1", 100.0), _sub("B", "b2", 100.0, status="rejected")]

    routes._attach_binding(status, subs)

    assert status["binding"]["subscription"] == "A"
    assert "next_usable" not in status["binding"]  # template says so explicitly


def test_binding_absent_leaves_status_untouched():
    status = {"level": "healthy", "binding": None}
    subs = [_sub("A", "b1", 10.0)]
    routes._attach_binding(status, subs)
    assert status["binding"] is None
    assert "is_binding" not in subs[0]


def test_unmatched_bearer_still_reports_a_way_out():
    """A bearer with no configured credential file (raw API key) has no row."""
    status = {"binding": {"bearer_id": "unknown", "window": "7d", "pct": 99, "retry_after": None}}
    subs = [_sub("B", "47f0b262", 12.0)]

    routes._attach_binding(status, subs)

    assert "subscription" not in status["binding"]  # template falls back to the hash
    assert status["binding"]["next_usable"] == "B"


def test_a_spent_weekly_budget_is_not_clear():
    """The 7d window was never read by the status strip.

    Two of three fleet accounts sit 7d-rejected mid-week, and the page called
    them clear while the binding line beside it read 100%.
    """
    spent = {
        "unified": {"util_5h": 0.1, "status_5h": "allowed", "util_7d": 1.0, "status_7d": "rejected"}
    }
    assert routes._bearer_pacing_state(spent)[0] == "throttled"

    pacing = {
        "unified": {"util_5h": 0.1, "status_5h": "allowed", "util_7d": 0.93, "status_7d": "allowed"}
    }
    assert routes._bearer_pacing_state(pacing)[0] == "pacing"

    clear = {
        "unified": {"util_5h": 0.1, "status_5h": "allowed", "util_7d": 0.2, "status_7d": "allowed"}
    }
    assert routes._bearer_pacing_state(clear)[0] is None


def _refused(label, bearer, pct):
    row = _sub(label, bearer, pct)
    row["status"] = "refused"
    return row


def test_a_refused_credential_is_never_offered_as_the_way_out():
    """Measured live 05/08/2026: account A answers
    `403 oauth_not_allowed_for_organization` on every request and the proxy
    quarantines it — yet it read as 0% used, so the binding block named it as
    what takes traffic next.
    """
    status = {"binding": {"bearer_id": "b-blocked", "window": "7d", "pct": 80, "retry_after": None}}
    subs = [_sub("B", "b-blocked", 80.0), _refused("A", "b-dead", 0.0), _sub("C", "b-ok", 62.0)]

    routes._attach_binding(status, subs)

    assert status["binding"]["next_usable"] == "C"  # not the 0%-used dead one


def test_refused_outranks_every_other_account_verdict():
    account = {
        "credential": {"ok": False, "detail": "OAuth authentication is currently not allowed"},
        "locked_in": "2h",
        "seen": False,
        "token": {"state": "expired", "detail": "expired 3h ago"},
        "win7": {"rejected": True, "pct": 100},
    }
    state, detail = routes._account_status(account)
    assert state == "refused"
    assert "not allowed" in detail


def test_quarantine_restored_after_a_restart_still_reaches_the_row(monkeypatch):
    """A restart restores the quarantine into `_restored_credentials` and leaves
    `bearer_state` clean. Reading bstate alone showed a 403-refused account as
    healthy — measured live 05/08/2026, right after an activation restart the
    page offered the org-dead account as "takes traffic next".
    """
    from anthropic_throttle_proxy import config, proxy

    bid = "b144f62f"
    config.bearer_state[bid] = {"inflight": 0, "queued": 0, "served": 0}  # no credential key
    proxy._restored_credentials[bid] = {
        "ok": False,
        "status": 403,
        "reason": "oauth_not_allowed_for_organization",
    }
    try:
        assert proxy._bearer_credential(bid).get("ok") is False
        state, _ = routes._account_status({"credential": proxy._bearer_credential(bid) or None})
        assert state == "refused"
    finally:
        config.bearer_state.pop(bid, None)
        proxy._restored_credentials.pop(bid, None)
