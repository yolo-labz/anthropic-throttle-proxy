"""Truth regressions for the status strip and provider rows (findings 1/2/3/11).

A refused credential or a supplied disabled admission can no longer render as
HEALTHY; missing credential/admission evidence reads as unknown (idle), never
as cleared; a credential that is empty, ok=None or malformed is NOT capacity
evidence; a central HTTP status is tier availability and NEVER DNS evidence
(only an explicitly supplied boolean counts, truthy strings never coerce);
custom hosts and IPs keep their full identity instead of truncating.
"""

import contextlib as _contextlib

import pytest

from anthropic_throttle_proxy import fleet
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


def test_sibling_missing_egress_field_is_unmeasured_not_failed():
    """Review B3 through the REAL integration path.

    The sibling health body is parsed by ``fleet._parse_health`` before it
    reaches ``_build_providers``. A body that OMITS ``upstream_egress_ok``
    is unmeasured (None → the template renders "DNS unmeasured") — the old
    ``bool(body.get(..., False))`` turned absent evidence into a measured
    "DNS failed" on every sibling that simply doesn't report the field, and
    the old tests bypassed the parser so the lie stayed invisible.
    """
    parsed = fleet._parse_health({"ok": True, "status": 200, "inflight": 0})
    assert parsed["upstream_egress_ok"] is None
    parsed["name"] = "sibling-a"
    rows = _provider_rows(fleet=[parsed], central_url="http://sib:9000", central_status="up")
    assert _row_named(rows, "sibling-a")["egress_ok"] is None

    # An explicit False is still a measured failure — only ABSENCE is unknown.
    measured = {
        **fleet._parse_health(
            {"ok": True, "status": 200, "inflight": 0, "upstream_egress_ok": False}
        ),
        "name": "sibling-b",
    }
    rows2 = _provider_rows(fleet=[measured], central_url="http://sib:9000", central_status="up")
    assert _row_named(rows2, "sibling-b")["egress_ok"] is False


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


# ── cross-family review, 18/09/2026 — five reproduced defects ────────────────
#
# The review that gated this branch found four MAJORs and two MINORs, each with
# a reproduction. Every one of them is a case of the page asserting more than
# its evidence supports, which is what this file exists to prevent — so each
# gets a regression here rather than a note in a comment.


def _subscription_row(account: dict) -> dict:
    """One Anthropic row through the REAL builder, not a hand-built dict."""
    return routes._build_subscriptions([account], {"lanes": [], "registry": []}, NOW)[0]


def _account(**over: object) -> dict:
    base = {
        "label": "A",
        "email": "a@example.test",
        "bearer_id": "b-dead",
        "seen": True,
        "src": "endpoint",
        "win5": {"pct": 12, "reset_in": "1h", "rejected": False},
        "win7": {"pct": 12, "reset_in": "2d", "rejected": False},
        "credential": {"ok": True, "status": 200, "reason": "", "detail": ""},
        "pace": 0.5,
        "eta": "",
    }
    base.update(over)
    return base


def test_a_failed_usage_endpoint_disqualifies_the_routing_recommendation():
    """MAJOR 1. `_account_status` files an endpoint failure as a NOTE and keeps
    the verdict at `ok`, so an account whose own usage call answered
    `credential rejected (401)` still ranked as the freest lane and was offered
    as "takes traffic next" on the strength of a percentage the page could not
    actually read. A recommendation is a claim; the evidence has to carry it.
    """
    healthy = _subscription_row(_account())
    assert healthy["status"] == "ok"
    assert healthy["routing_eligible"] is True

    broken = _subscription_row(
        _account(endpoint_err="credential rejected (401)", win7={"pct": 3, "reset_in": "2d"})
    )
    # The verdict is still `ok` — the endpoint error is a note, which is the
    # reason the recommendation needs its own gate rather than reusing status.
    assert broken["status"] == "ok"
    assert "credential rejected (401)" in broken["detail"]
    assert broken["routing_eligible"] is False


@pytest.mark.parametrize(
    "disqualifier",
    [
        {"endpoint_err": "credential rejected (401)"},
        {"error": "usage parse failed"},
        {"locked_in": "2h"},
        {"credential": {"ok": False, "detail": "oauth_not_allowed_for_organization"}},
        {"token": {"state": "expired", "detail": "expired 3h ago"}},
    ],
)
def test_every_untrustworthy_evidence_signal_vetoes_the_recommendation(disqualifier):
    row = _subscription_row(_account(**disqualifier))
    assert row["routing_eligible"] is False, (
        f"{disqualifier} left the row eligible to be recommended"
    )


def _binding_context(sibling: dict) -> dict:
    """A binding on account A plus one sibling, in the shape the builder emits."""
    status = {"binding": {"bearer_id": "bind", "window": "7d", "pct": 100, "evidence": "throttled"}}
    rows = [
        {
            "id": "blocked-lane",
            "bearer_id": "bind",
            "family": "anthropic",
            "status": "ok",
            "meters": [{"label": "7d", "pct": 100, "reset_in": "1h"}],
            "routing_eligible": True,
        },
        sibling,
    ]
    routes._attach_binding(status, rows)
    return status["binding"]


@pytest.mark.parametrize(
    ("name", "sibling", "expect_unknown"),
    [
        # "ok" but nothing about it could be verified: a fact we cannot state.
        (
            "unreadable",
            {
                "id": "unreadable-sibling",
                "bearer_id": "b2",
                "family": "anthropic",
                "status": "ok",
                "meters": [{"label": "7d", "pct": 4}],
                "routing_eligible": False,
            },
            True,
        ),
        # Every sibling is genuinely closed: a fact we can.
        (
            "refusing",
            {
                "id": "refused-sibling",
                "bearer_id": "b2",
                "family": "anthropic",
                "status": "refused",
                "meters": [],
                "routing_eligible": False,
            },
            False,
        ),
    ],
)
def test_an_unverified_sibling_is_never_reported_as_a_blocked_one(
    name: str, sibling: dict, expect_unknown: bool
):
    """The page used to answer "nothing — every sibling is blocked too" for
    BOTH "every sibling is refusing" and "I could not read any sibling".
    Those are different facts and only one of them is knowable here."""
    bound = _binding_context(sibling)
    assert "next_usable" not in bound
    assert bool(bound.get("next_usable_unknown")) is expect_unknown, name


def test_pacing_is_not_rendered_as_a_blocked_subscription():
    """MAJOR 2. `_compute_status` builds a binding object for PACING too — a
    window past the warn line that upstream still reports as `allowed` — and the
    strip said `blocked` with a `reopens in`, turning queue pressure into a
    quota refusal. The measured reason is on the object as `evidence`; the
    wording has to follow it.
    """
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(routes._TEMPLATES)), autoescape=True
    )

    def render(evidence: str) -> str:
        return env.get_template("partials/stats.html").render(
            subscriptions=[],
            bearers=[],
            providers=[],
            signals=[],
            lanes=None,
            last_advisor=None,
            served=0,
            inflight=0,
            queued=0,
            holds=0,
            retries=0,
            disconnects=0,
            status={
                "level": "pacing" if evidence == "pacing" else "throttled",
                "verdict": "PACING" if evidence == "pacing" else "THROTTLED",
                "since": "4m",
                "detail": "",
                "binding": {
                    "bearer_id": "b1",
                    "subscription": "A",
                    "sub": "a@example.test",
                    "window": "7d",
                    "pct": 30,
                    "resets_in": "1h",
                    "evidence": evidence,
                },
            },
        )

    pacing = render("pacing")
    # Scoped to the binding block: the phrase "blocked" also appears in the
    # no-sibling fallback text, which is a different sentence about a
    # different fact.
    assert '<span class="binding-label">binding constraint</span>' in pacing
    assert '<span class="binding-label">blocked</span>' not in pacing
    assert "reopens in" not in pacing
    assert "resets in 1h" in pacing

    throttled = render("throttled")
    assert '<span class="binding-label">blocked</span>' in throttled
    assert "reopens in 1h" in throttled


def test_an_unrepresentable_number_in_the_report_cannot_crash_the_reader(tmp_path, monkeypatch):
    """MAJOR 4. `math.isfinite` raises OverflowError on an int too large to
    convert to float, and this reads ARBITRARY JSON — so `intervalSeconds:
    10**400` in a file written by another process took down both dashboard
    endpoints, health JSON included. A number the platform cannot represent is
    not a reading.
    """
    from anthropic_throttle_proxy import lanes

    report = tmp_path / "lanes.json"
    report.write_text(
        '{"generatedAt": "2026-09-18T12:00:00Z", "intervalSeconds": 1' + "0" * 400 + ","
        ' "lanes": []}',
        encoding="utf-8",
    )
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(report))
    monkeypatch.setattr(lanes, "_cache", None)

    view = lanes.view(NOW)  # must not raise
    assert view["interval_s"] is None
    assert view["stale"] is False  # an invalid cadence cannot certify staleness
    assert "sampling interval invalid" in view["error"]


def test_a_backward_clock_does_not_extend_the_lane_cache(tmp_path, monkeypatch):
    """MINOR 6. `now - cached < TTL` is TRUE for a negative elapsed time, so a
    clock that stepped backward (NTP, a restored snapshot, suspend/resume)
    served the previous snapshot without re-reading — freshness that heals
    itself only once wall time catches back up."""
    from anthropic_throttle_proxy import lanes

    report = tmp_path / "lanes.json"
    report.write_text('{"generatedAt": "2026-09-18T12:00:00Z", "lanes": []}', encoding="utf-8")
    monkeypatch.setenv("THROTTLE_LANES_FILE", str(report))
    monkeypatch.setattr(lanes, "_cache", None)

    lanes.view(NOW)
    assert lanes._cache is not None and lanes._cache[0] == NOW
    # Time moves BACKWARD by an hour: the cache must be treated as unusable,
    # not as "still warm".
    lanes.view(NOW - 3600)
    assert lanes._cache[0] == NOW - 3600, "a backward clock reused the cached snapshot"


@_contextlib.asynccontextmanager
async def _ui_client():
    """A real aiohttp app with only the UI routes attached."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    routes.attach_ui(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def test_a_tab_rendered_by_an_older_build_is_told_to_reload(monkeypatch):
    """MAJOR 3. The panel emitted `data-revision` / `data-local` and nothing
    consumed them, so a tab left open across a deploy kept the old stylesheet,
    header, settings panel and footer while receiving new markup — and toggling
    `show_primary` left a projected panel inside a page that no longer matched
    it. The poll now reports the revision it was rendered with and the server
    answers `HX-Refresh` when it stops matching.
    """
    import asyncio

    async def run() -> None:
        async with _ui_client() as client:
            # Same revision → a normal partial, no reload requested.
            fresh = await client.get(f"/ui/stats?rev={routes._ASSET_V}")
            assert fresh.status == 200
            assert "HX-Refresh" not in fresh.headers

            # Older revision → the whole page reloads (new CSS URL and all).
            stale = await client.get("/ui/stats?rev=deadbeef0000")
            assert stale.status == 200  # still a valid partial render
            assert stale.headers.get("HX-Refresh") == "true"

            # A client that never declared a revision has nothing to be stale
            # against — a curl probe must not be told to reload.
            bare = await client.get("/ui/stats")
            assert bare.status == 200
            assert "HX-Refresh" not in bare.headers

            # Display mode is the same class of divergence on the same
            # mechanism. `show_local` defaults to true here, so a tab that
            # declares `local=false` was rendered under a mode the server no
            # longer agrees with.
            flipped = await client.get("/ui/stats?local=false")
            assert flipped.headers.get("HX-Refresh") == "true"

            # ...and a tab that declares the mode the server IS in stays put.
            agreed = await client.get("/ui/stats?local=true")
            assert "HX-Refresh" not in agreed.headers

    asyncio.run(run())


def test_the_advisor_reads_the_unfiltered_snapshot(monkeypatch):
    """MINOR 5. `_collect_view` applied the display projection, so with
    `show_primary: false` the advisor received an empty bearer list and a
    placeholder verdict — and diagnosed a page that does not exist. Hiding the
    button never disabled the endpoint.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from anthropic_throttle_proxy import (
        accounts,
        config,
        copilot,
        fleet,
        fleet_ui_config,
        lanes,
    )

    seen = []

    async def fake_recommend(snapshot):
        seen.append(snapshot)
        return {"text": "ok", "error": None, "trigger": "test"}

    monkeypatch.setenv("ADVISOR_ENABLED", "true")
    monkeypatch.setattr(
        fleet_ui_config, "load", lambda *a, **k: {"defaults": {"show_primary": False}}
    )

    synthetic = {
        "bearer_id": "sample01",
        "inflight": 0,
        "queued": 0,
        "served": 1,
        "credential": {"ok": False, "reason": "refused"},
    }

    async def run() -> None:
        async with _ui_client() as client:
            with (
                patch.dict(config.bearer_state, {"sample01": synthetic}, clear=True),
                patch.object(accounts, "bearer_labels", return_value={}),
                patch.object(accounts, "refresh_endpoint", new=AsyncMock(return_value={})),
                patch.object(accounts, "account_view", return_value=[]),
                patch.object(accounts, "identity_state", return_value={}),
                patch.object(fleet, "refresh", new=AsyncMock(return_value=[])),
                patch.object(copilot, "refresh", new=AsyncMock(return_value=[])),
                patch.object(lanes, "view", return_value=dict(lanes.EMPTY)),
                patch.object(routes, "_publish_account_gauges"),
                patch.object(routes, "_publish_lane_gauges"),
                patch("anthropic_throttle_proxy.ui.advisor_impl.recommend", fake_recommend),
            ):
                await client.post("/ui/advisor")

    asyncio.run(run())
    assert seen, "the advisor never ran"
    snapshot = seen[0]
    assert snapshot["status"]["verdict"] != "SUBSCRIPTIONS", (
        "the advisor was handed the display placeholder instead of the real verdict"
    )
    assert snapshot["bearers"], "the advisor was handed a view with the bearers hidden"
