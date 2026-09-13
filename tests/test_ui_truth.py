"""Truth regressions for the status strip and provider rows (findings 1/2/3/11).

A refused credential or a supplied disabled admission can no longer render as
HEALTHY; missing credential/admission evidence reads as unknown (idle), never
as cleared; a credential that is empty, ok=None or malformed is NOT capacity
evidence; a central HTTP status is tier availability and NEVER DNS evidence
(only an explicitly supplied boolean counts, truthy strings never coerce);
custom hosts and IPs keep their full identity instead of truncating.
"""

import pytest

from anthropic_throttle_proxy.ui import routes

NOW = 1000.0

REFUSED = {
    "ok": False,
    "status": 403,
    "reason": "refused",
    "detail": "synthetic rejection",
}
USABLE = {"ok": True, "status": 200, "reason": "", "detail": ""}


def _bearer(bid: str, **extra: object) -> dict:
    b = {"bearer_id": bid, "unified": {}, "queued": 0, "limiter": None}
    b.update(extra)
    return b


def _provider_rows(**over: object) -> list[dict]:
    base = dict(
        upstream="https://api.anthropic.com",
        central_url="(direct)",
        central_status="unknown",
        level="throttled",
        inflight=1,
        queued=0,
        served=3,
        max_concurrent=2,
        fleet=[],
    )
    base.update(over)
    return routes._build_providers(**base)


def _row_named(rows: list[dict], name: str) -> dict:
    """Locate one provider row by its stable name — never by table position."""
    return next(r for r in rows if r["name"] == name)


# --- findings 1/11: the headline may not claim capacity it cannot see --------


def test_refused_credential_on_unpaced_bearer_is_crit_not_healthy():
    status = routes._compute_status(
        [_bearer("deadbeef")], "fair", NOW, credential_verdicts={"deadbeef": REFUSED}
    )
    assert status["level"] == "crit"
    assert status["verdict"] == "CRIT"
    assert status["refused"] == ["deadbeef"]
    assert "refused/disabled" in status["detail"]
    assert "clear" not in status["detail"]


def test_refusal_inside_the_bearer_dict_counts_without_the_mapping():
    status = routes._compute_status([_bearer("deadfeed", credential=REFUSED)], "fair", NOW)
    assert status["level"] == "crit"
    assert status["refused"] == ["deadfeed"]


def test_mixed_usable_and_refused_never_claims_all_clear():
    status = routes._compute_status(
        [_bearer("aaaaaaaa"), _bearer("bbbbbbbb")],
        "fair",
        NOW,
        credential_verdicts={"aaaaaaaa": USABLE, "bbbbbbbb": REFUSED},
    )
    assert status["level"] == "crit"
    assert "1 of 2" in status["detail"]
    assert "all 2 bearers clear" not in status["detail"]


def test_supplied_disabled_admission_blocks_healthy():
    # Only a SUPPLIED authoritative False counts; nothing is invented here.
    status = routes._compute_status(
        [_bearer("aaaaaaaa")], "fair", NOW, admission={"aaaaaaaa": False}
    )
    assert status["level"] == "crit"
    assert status["refused"] == ["aaaaaaaa"]


def test_absent_credential_and_admission_evidence_reads_unknown_not_healthy():
    status = routes._compute_status([_bearer("aaaaaaaa")], "fair", NOW)
    assert status["level"] == "idle"
    assert status["verdict"] == "UNKNOWN"
    assert status["unevidenced"] == ["aaaaaaaa"]
    assert "unknown" in status["detail"].lower()
    assert "clear" not in status["detail"]


def test_positive_credential_evidence_still_reaches_healthy():
    status = routes._compute_status(
        [_bearer("aaaaaaaa")], "fair", NOW, credential_verdicts={"aaaaaaaa": USABLE}
    )
    assert status["level"] == "healthy"
    assert status["verdict"] == "HEALTHY"


# --- finding 1 regression: empty/unknown/malformed credentials prove nothing -


def test_empty_credential_dict_is_unevidenced_not_healthy():
    status = routes._compute_status(
        [_bearer("aaaaaaaa")], "fair", NOW, credential_verdicts={"aaaaaaaa": {}}
    )
    assert status["level"] == "idle"
    assert status["verdict"] == "UNKNOWN"
    assert status["unevidenced"] == ["aaaaaaaa"]
    assert status["refused"] == []


def test_ok_none_credential_is_unevidenced_not_healthy():
    status = routes._compute_status(
        [_bearer("aaaaaaaa")], "fair", NOW, credential_verdicts={"aaaaaaaa": {"ok": None}}
    )
    assert status["level"] == "idle"
    assert status["unevidenced"] == ["aaaaaaaa"]


@pytest.mark.parametrize(
    "malformed",
    ["ok", 42, ["ok"], {"status": 200, "detail": "seems fine"}, {"ok": 1}, None],
)
def test_malformed_credential_never_manufactures_health(malformed):
    status = routes._compute_status(
        [_bearer("aaaaaaaa")], "fair", NOW, credential_verdicts={"aaaaaaaa": malformed}
    )
    assert status["level"] == "idle", malformed
    assert status["refused"] == [], malformed
    assert status["unevidenced"] == ["aaaaaaaa"], malformed


def test_explicit_ok_true_is_evidence_but_malformed_neighbour_is_not():
    status = routes._compute_status(
        [_bearer("aaaaaaaa"), _bearer("bbbbbbbb")],
        "fair",
        NOW,
        credential_verdicts={"aaaaaaaa": {"ok": True}, "bbbbbbbb": {"detail": "no verdict"}},
    )
    assert status["unevidenced"] == ["bbbbbbbb"]
    assert status["refused"] == []
    # One unevidenced lane keeps the strip honest — no wholesale HEALTHY.
    assert status["level"] == "idle"
    assert status["verdict"] == "UNKNOWN"


def test_explicit_true_admission_counts_as_capacity_evidence():
    status = routes._compute_status(
        [_bearer("aaaaaaaa")], "fair", NOW, admission={"aaaaaaaa": True}
    )
    assert status["level"] == "healthy"
    assert status["unevidenced"] == []


def test_pacing_still_wins_over_unknown_evidence():
    # queued work → pacing without depending on the utilisation threshold.
    status = routes._compute_status([_bearer("aaaaaaaa", queued=2)], "fair", NOW)
    assert status["level"] == "pacing"


def test_refusal_worst_wins_over_throttled_and_names_both():
    throttled = _bearer("bbbbbbbb", last_ratelimit={"retry-after": "120"})
    status = routes._compute_status(
        [_bearer("aaaaaaaa"), throttled],
        "fair",
        NOW,
        credential_verdicts={"aaaaaaaa": REFUSED},
    )
    assert status["level"] == "crit"
    assert "1 throttled" in status["detail"]


def test_credential_state_arguments_are_keyword_only():
    with pytest.raises(TypeError):
        routes._compute_status([_bearer("aaaaaaaa")], "fair", NOW, {"a": REFUSED})


def test_headline_scope_is_explicitly_local():
    status = routes._compute_status([_bearer("aaaaaaaa", credential=USABLE)], "fair", NOW)
    assert status["scope"] == "local"
    assert status["detail"].endswith("· local proxy view")


# --- finding 3: identity is never truncated to "127" -------------------------


def test_provider_label_keeps_full_ip_identity():
    assert routes._provider_label("http://127.0.0.1:8766") == "127.0.0.1"
    assert routes._provider_label("http://[::1]:8766") == "::1"
    assert routes._provider_label("http://192.168.7.20:9000") == "192.168.7.20"


def test_provider_label_still_collapses_known_vendor_hosts():
    assert routes._provider_label("https://api.anthropic.com") == "anthropic"
    assert routes._provider_label("https://api.moonshot.ai/anthropic") == "moonshot"
    assert routes._provider_label("") == "upstream"  # defensive, never raises


def test_provider_label_keeps_full_custom_hostname():
    # ONLY documented provider hosts collapse — a custom/internal host keeps
    # its full name so distinct lanes stay distinguishable.
    assert routes._provider_label("http://sibling-lab.internal:9000") == "sibling-lab.internal"
    assert routes._provider_label("https://proxy.corp.example.com/v1") == "proxy.corp.example.com"
    assert routes._provider_label("http://kimi-gateway.lan") == "kimi-gateway.lan"


def test_provider_label_collapses_only_the_documented_provider_hosts():
    assert routes._provider_label("https://api.openai.com/v1") == "openai"
    assert routes._provider_label("https://api.z.ai/api/paas/v4") == "z.ai"


def test_provider_label_never_crashes_on_malformed_bracketed_host():
    # urlparse raises ValueError for an unclosed bracket; the label must fall
    # back to the raw string instead of raising into the render path.
    assert routes._provider_label("http://[::1") == "http://[::1"
    assert isinstance(routes._provider_label("http://[gg::1]"), str)
    assert routes._provider_label("not a url") == "not a url"


# --- finding 2: DNS is DNS, never auth/inference verification ----------------


def test_direct_primary_reports_dns_unknown_not_ok():
    p = _row_named(_provider_rows(), "anthropic")
    assert p["dns_ok"] is None
    assert p["egress_ok"] is None  # compat alias carries the honest value
    assert "unverified" in p["dns_note"]


def test_central_http_status_is_never_dns_evidence():
    """A central tier HTTP status answers 'is the tier up', not 'does DNS work'.

    The dashboard process ran no resolver: up, down or unknown, the primary's
    dns/egress verdict stays None — DNS unknown is INDEPENDENT of up/down.
    """
    for central_status in ("up", "down", "whatever"):
        p = _row_named(
            _provider_rows(central_url="http://central:9000", central_status=central_status),
            "central",
        )
        assert p["dns_ok"] is None, central_status
        assert p["egress_ok"] is None, central_status
        assert "DNS" in p["dns_note"], central_status


def test_explicit_supplied_boolean_is_the_only_primary_dns_source():
    ok = _row_named(
        _provider_rows(
            central_url="http://central:9000", central_status="up", upstream_dns_ok=True
        ),
        "central",
    )
    assert ok["dns_ok"] is True
    assert ok["egress_ok"] is True  # compat alias carries the same verdict
    failed = _row_named(
        _provider_rows(central_url="(direct)", central_status="unknown", upstream_dns_ok=False),
        "anthropic",
    )
    assert failed["dns_ok"] is False


def test_nonboolean_dns_values_never_become_bool_claims():
    # A truthy STRING is not a probe result — bool("up") manufactured evidence.
    p = _row_named(_provider_rows(upstream_dns_ok="up"), "anthropic")
    assert p["dns_ok"] is None and p["egress_ok"] is None
    fleet = [
        {
            "name": "kimi",
            "ok": True,
            "upstream": "https://api.moonshot.ai",
            "upstream_egress_ok": "yes",
        }
    ]
    kimi = _row_named(_provider_rows(fleet=fleet), "kimi")
    assert kimi["dns_ok"] is None and kimi["egress_ok"] is None


def test_dns_ok_true_never_upgrades_a_dead_key():
    fleet = [
        {
            "name": "kimi",
            "ok": True,
            "upstream": "https://api.moonshot.ai",
            "served": 0,
            "inflight": 0,
            "queued": 0,
            "max_concurrent": 6,
            "upstream_egress_ok": True,
            "upstream_auth_ok": False,
            "upstream_auth_error": "401 invalid key",
        }
    ]
    kimi = _row_named(_provider_rows(fleet=fleet), "kimi")
    assert kimi["dns_ok"] is True  # reachability signal present…
    assert kimi["auth_dead"] is True
    assert kimi["level"] == "crit"  # …but the lane is still crit, never healthy
    assert "not auth/inference" in kimi["dns_note"]


def test_missing_dns_state_stays_unknown_not_false():
    fleet = [
        {
            "name": "glm",
            "ok": False,
            "upstream": "http://127.0.0.1:8766",
            "err": "unreachable",
        }
    ]
    glm = _row_named(_provider_rows(fleet=fleet), "glm")
    assert glm["dns_ok"] is None
    assert glm["egress_ok"] is None
