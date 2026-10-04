"""305 — trusted admission display: local predicate only, no fabricated health.

Contract (specs/305-admission-display/contract.md): the admission display uses
the EXISTING local admission predicate only, performs no network I/O on the
render path, never fabricates health and never revives genuinely rejected
capacity. Account B reconciles as: real binding + local admission exist, the
real client path is still NOT ACCEPTED — the display must say exactly that
(and must not fall back to the old B-unknown snapshot wording). These tests
are synthetic; no credentials, provider calls or private account evidence.
"""

from __future__ import annotations

import socket

from anthropic_throttle_proxy.ui import routes


def _bearer(bid: str, *, credential=None, queued: int = 0) -> dict:
    return {
        "bearer_id": bid,
        "credential": credential,
        "unified": None,
        "last_ratelimit": None,
        "limiter": {},
        "queued": queued,
    }


def _status(
    bearers,
    *,
    credential_verdicts=None,
    admission=None,
    client_paths=None,
    bindings=None,
):
    return routes._compute_status(
        bearers,
        "fair",
        credential_verdicts=credential_verdicts,
        admission=admission,
        client_paths=client_paths,
        bindings=bindings,
    )


def test_locally_admitted_bound_bearer_says_client_path_not_accepted():
    """Conta B reconciliation: binding + local admission exist, the real client
    path is still unaccepted — display says exactly that, never B-unknown."""
    bearer = _bearer("07c7ae8a")
    status = _status([bearer], admission={"07c7ae8a": True}, bindings={"07c7ae8a": True})
    assert "client path not accepted" in status["detail"]
    assert "capacity unknown" not in status["detail"]
    assert status["verdict"] == "NOT-ACCEPTED"
    assert status["level"] != "healthy"


def test_refused_capacity_is_never_revived_by_admission():
    """A genuinely rejected credential stays CRIT — local admission must never
    revive rejected Anthropic capacity in the display."""
    bearer = _bearer("b144f62f", credential={"ok": False, "status": 403, "reason": "streak-3"})
    status = _status([bearer], admission={"b144f62f": True})
    assert status["level"] == "crit"
    assert "refused/disabled" in status["detail"]


def test_unevidenced_bearer_stays_capacity_unknown():
    """No credential verdict and no admission verdict proves nothing — the
    finding-11 unknown wording stays for genuinely unevidenced bearers."""
    bearer = _bearer("stranger")
    status = _status([bearer])
    assert status["verdict"] == "UNKNOWN"
    assert "capacity unknown" in status["detail"]


def test_real_message_credential_is_client_acceptance():
    """``ok: True`` exists only when a real message reopened the account —
    that is client-path evidence, and the strip may say healthy."""
    bearer = _bearer("666a53af", credential={"ok": True})
    status = _status([bearer], admission={"666a53af": True}, bindings={"666a53af": True})
    assert status["level"] == "healthy"
    assert "clear" in status["detail"]


def test_explicit_client_acceptance_from_sanitized_map_is_honoured():
    """An explicit True from the sanitized map is acceptance evidence; anything
    absent or non-True stays not-accepted (never fabricate health)."""
    bearer = _bearer("07c7ae8a")
    accepted = _status(
        [bearer],
        admission={"07c7ae8a": True},
        client_paths={"07c7ae8a": True},
        bindings={"07c7ae8a": True},
    )
    assert accepted["level"] == "healthy"
    unknown_flag = _status(
        [bearer],
        admission={"07c7ae8a": True},
        client_paths={"07c7ae8a": False},
        bindings={"07c7ae8a": True},
    )
    assert "client path not accepted" in unknown_flag["detail"]


def test_unbound_admitted_bearer_keeps_legacy_admission_reading():
    """Boundary: a bearer with no meter binding keeps the legacy reading —
    the not-accepted line is scoped to bound seats (Conta B reconciliation)."""
    bearer = _bearer("unbound")
    status = _status([bearer], admission={"unbound": True})
    assert status["level"] == "healthy"
    assert "client path not accepted" not in status["detail"]


def test_status_computation_is_io_free():
    """Render-path trust: computing the status must not touch sockets or HTTP."""
    original = socket.socket

    def forbidden(*args, **kwargs):
        raise AssertionError("status computation touched the network")

    socket.socket = forbidden
    try:
        status = _status([_bearer("x", credential={"ok": True})], admission={"x": True})
    finally:
        socket.socket = original
    assert status["level"] == "healthy"


def test_status_output_carries_no_private_account_evidence():
    """Detail strings are counts and fixed phrases only — never credential
    detail, account paths or other private evidence."""
    bearer = _bearer(
        "leaky",
        credential={"ok": False, "status": 403, "detail": "user@example.com /home/secret.json"},
    )
    status = _status([bearer])
    rendered = str(status)
    assert "user@example.com" not in rendered
    assert "/home/secret.json" not in rendered
