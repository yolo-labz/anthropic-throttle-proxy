"""305 — trusted admission display: the existing local admission predicate only.

Real contract (coordinator correction 04/10/2026): the ONLY new source is the
existing local admission predicate feeding the EXISTING status vocabulary.
Positive local admission keeps its existing local meaning and is never
converted into UNKNOWN or non-healthy; a genuine rejection always wins. There
is NO new runtime verdict — "client path not accepted" is a project-receipt /
static route-matrix fact (specs/305-admission-display/route-matrix.md), never
runtime state. Counts describe the VISIBLE bearer set (true scope stated in
the matrix). Synthetic fixtures only: no credentials, no provider calls, no
private account evidence.
"""

from __future__ import annotations

from types import SimpleNamespace

from anthropic_throttle_proxy import fleet_ui_config
from anthropic_throttle_proxy.ui import routes

_NEW_VERDICT_WORDS = ("NOT-ACCEPTED", "client path not accepted", "locally admitted")


def _bearer(bid: str, *, credential=None, queued: int = 0) -> dict:
    return {
        "bearer_id": bid,
        "credential": credential,
        "unified": None,
        "last_ratelimit": None,
        "limiter": {},
        "queued": queued,
    }


def _status(bearers, *, credential_verdicts=None, admission=None):
    return routes._compute_status(
        bearers,
        "fair",
        credential_verdicts=credential_verdicts,
        admission=admission,
    )


def test_locally_admitted_bearer_keeps_existing_local_meaning():
    """Positive local admission keeps its EXISTING meaning — it is never
    converted into UNKNOWN or non-healthy (Conta B's false B-unknown dies)."""
    bearer = _bearer("07c7ae8a")
    status = _status([bearer], admission={"07c7ae8a": True})
    assert status["verdict"] != "UNKNOWN"
    assert "capacity unknown" not in status["detail"]
    assert status["level"] == "healthy"
    for word in _NEW_VERDICT_WORDS:
        assert word not in str(status)


def test_genuine_rejection_wins_over_positive_admission():
    """A genuinely rejected credential stays CRIT — positive admission must
    never revive rejected Anthropic capacity in the display."""
    bearer = _bearer("b144f62f", credential={"ok": False, "status": 403, "reason": "streak-3"})
    status = _status([bearer], admission={"b144f62f": True})
    assert status["level"] == "crit"
    assert "refused/disabled" in status["detail"]


def test_admission_false_is_a_refusal():
    bearer = _bearer("x")
    status = _status([bearer], admission={"x": False})
    assert status["level"] == "crit"
    assert "refused/disabled" in status["detail"]


def test_unevidenced_bearer_stays_capacity_unknown():
    """No credential verdict and no admission verdict proves nothing — the
    finding-11 unknown wording is unchanged for genuinely unevidenced bearers."""
    bearer = _bearer("stranger")
    status = _status([bearer])
    assert status["verdict"] == "UNKNOWN"
    assert "capacity unknown" in status["detail"]


def test_no_invented_runtime_verdict_vocabulary():
    """Client-path-unaccepted is a receipt/matrix fact only: no runtime state
    of this display may ever speak it."""
    creds = (None, {"ok": True}, {"ok": False, "reason": "refused"})
    for cred in creds:
        for admitted in (None, True, False):
            admission = {"b": admitted} if admitted is not None else None
            status = _status([_bearer("b", credential=cred)], admission=admission)
            rendered = str(status)
            for word in _NEW_VERDICT_WORDS:
                assert word not in rendered


def test_counts_describe_the_visible_bearer_set():
    """True scope: the counts are of the VISIBLE bearer set (the matrix states
    this is a local, visible subset — never a fleet-wide claim)."""
    bearers = [_bearer(f"b{i}") for i in range(3)]
    status = _status(bearers, admission={f"b{i}": True for i in range(3)})
    assert "3 of 3" in status["detail"] or "all 3 bearers clear" in status["detail"]


async def test_real_call_site_uses_the_existing_local_admission_predicate(monkeypatch):
    """The real `_collect_view` status wiring reads the existing predicate
    (bearer_usable ∧ ¬credential_dead ∧ meter_binding_allows) in-process — no
    provider scan, and the result feeds the EXISTING status vocabulary."""
    calls: list[tuple[str, bool]] = []

    async def fake_refresh(now):
        return {}

    async def fake_fleet(now):
        return []

    async def fake_copilot(now):
        return []

    monkeypatch.setattr(routes._accounts, "refresh_endpoint", fake_refresh)
    monkeypatch.setattr(routes._accounts, "account_view", lambda bearers, now, endpoint: [])
    monkeypatch.setattr(routes._accounts, "bearer_labels", lambda: {})
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda endpoint, identity: None)
    monkeypatch.setattr(routes._fleet, "refresh", fake_fleet)
    monkeypatch.setattr(routes._copilot, "refresh", fake_copilot)
    monkeypatch.setattr(routes._lanes, "view", lambda now: {"lanes": [], "registry": []})
    monkeypatch.setattr(
        fleet_ui_config,
        "load",
        lambda: {
            "subscriptions": [],
            "defaults": {"emoji_by_family": {}, "hidden_families": [], "show_primary": True},
        },
    )
    monkeypatch.setitem(
        routes._proxy.bearer_state, "07c7ae8a", {"inflight": 0, "queued": 0, "served": 1}
    )
    monkeypatch.setitem(
        routes._proxy.bearer_limiters,
        "07c7ae8a",
        SimpleNamespace(
            snapshot=lambda: {
                "queue_enabled": True,
                "priority_inflight": 0,
                "max_concurrent": 2,
                "inflight": 0,
                "queued_total": 0,
            }
        ),
    )
    monkeypatch.setattr(routes._proxy, "_bearer_credential", lambda bid: None)
    monkeypatch.setattr(
        routes._proxy, "_bearer_usable", lambda b, now: calls.append(("usable", True)) or True
    )
    monkeypatch.setattr(
        routes._proxy,
        "_bearer_credential_dead",
        lambda bid: calls.append(("dead", False)) or False,
    )
    monkeypatch.setattr(
        routes._proxy,
        "_meter_binding_allows",
        lambda bid, now=None: calls.append(("meter", True)) or True,
    )
    result = await routes._collect_view()
    status = result["status"]
    # The predicate WAS consulted per factor for the visible bearer.
    assert ("usable", True) in calls and ("dead", False) in calls and ("meter", True) in calls
    # Its positive meaning is the existing one: not UNKNOWN, no new verdict.
    assert status["verdict"] != "UNKNOWN"
    for word in _NEW_VERDICT_WORDS:
        assert word not in str(status)

    # A genuine refusal from the same predicate still wins (never revived).
    monkeypatch.setattr(routes._proxy, "_bearer_credential", lambda bid: {"ok": False})
    result = await routes._collect_view()
    assert result["status"]["level"] == "crit"
    assert "refused/disabled" in result["status"]["detail"]
