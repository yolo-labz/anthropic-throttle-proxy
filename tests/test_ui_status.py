"""Status-strip binding-window labelling + stale-window handling.

Regression for the 19/07/2026 dashboard bug: the THROTTLED banner hardcoded
"5h window" and read a reset (stale) 5h utilisation, so a bearer whose binding
window was the 7d (representative_claim=seven_day, 87%) with an already-reset 5h
(86%) rendered as "binding: 5h window 86%" — contradicting the accounts panel
that correctly showed the 5h window as "0% · reset".
"""

# The panel renderers live in `ui_render.py` so this suite and
# `test_ui_layout.py` exercise the SAME template context rather than two copies
# of it — a context kept in two places drifts, and then one suite quietly stops
# testing the panel the other one does.
from ui_render import render_meter as _render_meter
from ui_render import render_stats as _render_stats
from ui_render import render_subscription as _render_subscription

from anthropic_throttle_proxy.ui import routes

NOW = 1000.0
PAST = 500.0  # reset epoch already elapsed relative to NOW → stale reading
FUTURE = 2000.0  # window still open


def _bearer(bid: str, unified: dict) -> dict:
    return {"bearer_id": bid, "unified": unified, "queued": 0, "limiter": None}


def test_window_stale_by_reset_epoch():
    assert routes._window_stale({"reset_5h": PAST}, "reset_5h", NOW) is True
    assert routes._window_stale({"reset_5h": FUTURE}, "reset_5h", NOW) is False
    assert routes._window_stale({}, "reset_5h", NOW) is False  # no reading → not stale
    assert routes._window_stale(None, "reset_5h", NOW) is False


def test_live_unified_drops_only_the_stale_window():
    unified = {
        "util_5h": 0.86,
        "reset_5h": PAST,  # rolled over → drop util_5h
        "util_7d": 0.87,
        "reset_7d": FUTURE,  # still open → keep util_7d
        "representative_claim": "seven_day",
    }
    live = routes._live_unified(unified, NOW)
    assert "util_5h" not in live
    assert live["util_7d"] == 0.87
    assert routes._live_unified(None, NOW) == {}
    assert routes._live_unified({}, NOW) == {}


def test_binding_line_names_the_representative_window_not_hardcoded_5h():
    # The exact incident: stale 5h 86% + live representative 7d 87%.
    bearers = [
        _bearer(
            "b144f62f",
            {
                "util_5h": 0.86,
                "reset_5h": PAST,
                "status_5h": "allowed",
                "util_7d": 0.87,
                "reset_7d": FUTURE,
                "status": "allowed_warning",
                "status_7d": "allowed_warning",
                "representative_claim": "seven_day",
            },
        )
    ]
    status = routes._compute_status(bearers, "fair", NOW)
    # The binding is an OBJECT now, not a clause: the strip and the binding
    # block rendered the same condition twice and could drift apart (#179).
    # 87% ≥ the 80% warn line is measured pacing evidence (review B2), so
    # the binding exists and names the LIVE representative window.
    assert status["binding"] == {
        "bearer_id": "b144f62f",
        "window": "7d",
        "pct": 87,
        "retry_after": None,
        "evidence": "pacing",
    }
    assert "5h" not in str(status["binding"]["window"])  # never the stale label


def test_binding_line_uses_5h_when_it_is_the_live_binding_window():
    bearers = [
        _bearer(
            "aaaa1111",
            {
                "util_5h": 0.91,
                "reset_5h": FUTURE,
                "util_7d": 0.40,
                "reset_7d": FUTURE,
                "representative_claim": "five_hour",
            },
        )
    ]
    status = routes._compute_status(bearers, "fair", NOW)
    assert status["binding"]["window"] == "5h"
    assert status["binding"]["pct"] == 91
    assert status["binding"]["bearer_id"] == "aaaa1111"
    assert status["binding"]["evidence"] == "pacing"


def test_binding_requires_measured_evidence_not_just_a_utilization_number():
    """Review B2 / finding 8: the highest utilization NUMBER is not "blocked".

    A 30% allowed_warning reading with no pushback, no rejection and no
    queue pressure is a healthy meter — it must not manufacture a binding
    claim, let alone a page that says "blocked".
    """
    bearers = [
        _bearer(
            "cccc3333",
            {
                "util_5h": 0.30,
                "reset_5h": FUTURE,
                "status": "allowed",
                "status_5h": "allowed",
                "representative_claim": "five_hour",
            },
        )
    ]
    status = routes._compute_status(bearers, "fair", NOW)
    assert status["binding"] is None


def test_binding_names_rejection_even_below_the_warn_line():
    # A rejected window is upstream's own answer — binding evidence even at
    # low utilization.
    bearers = [
        _bearer(
            "dddd4444",
            {
                "util_5h": 0.12,
                "reset_5h": FUTURE,
                "status": "rejected",
                "status_5h": "rejected",
                "representative_claim": "five_hour",
            },
        )
    ]
    status = routes._compute_status(bearers, "fair", NOW)
    assert status["binding"] is not None
    assert status["binding"]["evidence"] == "throttled"


def test_attach_binding_never_offers_unmeasured_accounts_as_next_usable():
    """Review B2: "takes traffic next" must be earned with measurements.

    An "unseen" account has no evidence of usability, and ranking missing
    meters as 0% made the page recommend exactly the account it knew
    nothing about.
    """
    status = {
        "binding": {
            "bearer_id": "bind0001",
            "window": "7d",
            "pct": 100,
            "retry_after": None,
            "evidence": "pacing",
        }
    }
    rows = [
        {
            "id": "measured-ok",
            "family": "anthropic",
            "status": "ok",
            "bearer_id": "aaaa1111",
            "meters": [{"label": "7d", "pct": 85}],
            "routing_eligible": True,
        },
        {
            "id": "unseen-no-evidence",
            "family": "anthropic",
            "status": "unseen",
            "meters": [],
        },
        {
            "id": "ok-but-unmeasured",
            "family": "anthropic",
            "status": "ok",
            "meters": [{"label": "7d"}],  # meter present, pct missing
        },
        {
            "id": "refused",
            "family": "anthropic",
            "status": "refused",
            "meters": [{"label": "7d", "pct": 0}],
        },
    ]
    routes._attach_binding(status, rows)
    bound = status["binding"]
    assert bound["next_usable"] == "measured-ok"
    assert bound["next_usable_pct"] == 85
    # No row matched the binding bearer here, so none is flagged — and none
    # of the unmeasured rows may silently become the suggestion either.
    assert all("is_binding" not in row or row["is_binding"] is not True for row in rows)


def test_no_binding_line_when_all_windows_stale():
    bearers = [
        _bearer(
            "bbbb2222",
            {"util_5h": 0.5, "reset_5h": PAST, "util_7d": 0.5, "reset_7d": PAST},
        )
    ]
    status = routes._compute_status(bearers, "fair", NOW)
    assert status["binding"] is None
    assert "binding" not in status["detail"]


def test_provider_label_derives_host_root():
    assert routes._provider_label("https://api.anthropic.com") == "anthropic"
    assert routes._provider_label("https://api.moonshot.ai/anthropic") == "moonshot"
    assert (
        routes._provider_label("http://127.0.0.1:8766") == "127.0.0.1"
    )  # full IPv4 — finding 3: a truncated "127" hid which loopback lane
    assert routes._provider_label("http://[::1]:8766") == "::1"  # bracketed IPv6, full
    assert routes._provider_label("") == "upstream"  # defensive: never raises / empty


def _providers(**over):
    base = dict(
        upstream="https://api.anthropic.com",
        central_url="(direct)",
        central_status="unknown",
        level="throttled",
        inflight=10,
        queued=9,
        served=23550,
        max_concurrent=5,
        fleet=[],
    )
    base.update(over)
    return routes._build_providers(**base)


def test_build_providers_always_has_primary_by_default():
    # "Integrate all providers by default": the primary lane renders with NO
    # fleet configured — the dead-lane env card is no longer the only provider row.
    rows = _providers(fleet=[])
    assert len(rows) == 1
    p = rows[0]
    assert p["kind"] == "primary"
    assert p["name"] == "anthropic"
    assert (
        p["ok"] is True and p["egress_ok"] is None
    )  # direct mode: no DNS probe — unknown, never a hardcoded ok
    assert p["served"] == 23550 and p["max_concurrent"] == 5
    assert p["level"] == "throttled"


def test_build_providers_central_http_status_is_not_dns():
    up = _providers(central_url="http://central:9000", central_status="up")[0]
    assert up["name"] == "central" and up["upstream"] == "http://central:9000"
    # Corrected finding 2: a central HTTP status is tier availability, never
    # DNS evidence — DNS stays unknown whether the tier is up or down.
    assert up["dns_ok"] is None and up["egress_ok"] is None
    down = _providers(central_url="http://central:9000", central_status="down")[0]
    assert down["dns_ok"] is None and down["egress_ok"] is None
    supplied = _providers(
        central_url="http://central:9000", central_status="up", upstream_dns_ok=True
    )[0]
    assert supplied["dns_ok"] is True  # only an explicit boolean may claim DNS


def test_build_providers_appends_fleet_siblings():
    fleet = [
        {
            "name": "kimi",
            "ok": True,
            "upstream": "https://api.moonshot.ai",
            "served": 18,
            "inflight": 0,
            "queued": 0,
            "max_concurrent": 6,
            "upstream_egress_ok": True,
        },
        {
            "name": "glm",
            "ok": False,
            "upstream": "http://127.0.0.1:8766",
            "err": "sibling unreachable",
        },
    ]
    rows = _providers(fleet=fleet)
    assert [r["name"] for r in rows] == ["anthropic", "kimi", "glm"]
    kimi = rows[1]
    assert kimi["kind"] == "sibling" and kimi["ok"] is True and kimi["served"] == 18
    assert kimi["level"] == "healthy"
    glm = rows[2]
    # A failed sibling probe maps to "idle" (grey), NOT "throttled" — a dead lane
    # must be visually distinct from the primary's real rate-limited state, which
    # is also "throttled". (Codex/Throttle #156 review, near-blocker.)
    assert glm["ok"] is False and glm["level"] == "idle" and glm["err"] == "sibling unreachable"
    assert glm["level"] != rows[0]["level"]  # dead sibling != rate-limited primary
    assert glm["served"] == 0  # missing numeric fields coerce to 0, never KeyError


def _rows(lane_ids_by_family, statuses=None):
    """Build subscription rows straight from _build_subscriptions."""
    from anthropic_throttle_proxy.ui import routes

    statuses = statuses or {}
    lanes_view = {
        "lanes": [
            {
                "id": lane_id,
                "kind": family,
                "family": family,
                "status": statuses.get(lane_id, "ok"),
                "meters": [{"label": "w", "used_pct": pct, "reset_in": "", "resets_at": None}],
                "reason": "",
            }
            for family, lane_id, pct in lane_ids_by_family
        ]
    }
    return routes._build_subscriptions([], lanes_view, 1_760_000_000.0)


def test_subscription_rows_group_by_family():
    """A single global fullest-first sort interleaved the providers.

    Live on 09/08 the order was `B · copilot · A · codex:b · C · codex:a`, so
    "how is Anthropic doing" could not be answered without reading every row.
    Families stay together; the most-pressed family still leads.
    """
    # Chosen so the two orders DISAGREE: a global fullest-first sort gives
    # codex:b(95) · copilot(50) · codex:a(10) — github wedged between the two
    # openai rows. Grouping keeps the openai pair adjacent.
    rows = _rows(
        [
            ("openai", "codex:a", 10.0),
            ("github", "copilot:personal", 50.0),
            ("openai", "codex:b", 95.0),
        ]
    )
    assert [r["id"] for r in rows] == ["codex:b", "codex:a", "copilot:personal"]


def test_zai_subscription_row_carries_provider_and_billing_metadata():
    from anthropic_throttle_proxy.ui import routes

    row = routes._build_subscriptions(
        [],
        {
            "lanes": [
                {
                    "id": "zai:plan",
                    "kind": "zai",
                    "provider": "Z.AI",
                    "icon": "✨",
                    "family": "chinese-frontier",
                    "status": "ok",
                    "plan": "Pro V3",
                    "meters": [
                        {
                            "label": "5h",
                            "used_pct": 1.0,
                            "reset_in": "1h 52m",
                            "resets_at": 1_760_006_720,
                            "window_mins": 300,
                        },
                        {
                            "label": "7d",
                            "used_pct": 2.0,
                            "reset_in": "5d 05h",
                            "resets_at": 1_760_450_000,
                            "window_mins": 10080,
                        },
                    ],
                    "billing": {
                        "current": True,
                        "plan_status": "VALID",
                        "auto_renew": True,
                        "cycle": "monthly",
                        "renewal_amount": 80.0,
                        "currency": "USD",
                        "next_renew_date": "2026-09-27",
                        "payment_type": "WAIT_PAY",
                    },
                    "reason": "",
                }
            ]
        },
        1_760_000_000.0,
    )[0]
    assert row["identity"] == "Z.AI"
    assert row["icon"] == "✨"
    assert row["billing"]["current"] is True
    assert {meter["label"] for meter in row["meters"]} == {"5h", "7d"}


def test_a_refusing_subscription_shows_no_burn_projection():
    """`1.08× · exhausts in <1m` beside a REJECTED badge predicts the past."""
    from anthropic_throttle_proxy.ui import routes

    lanes_view = {
        "lanes": [
            {
                "id": "codex:b",
                "kind": "codex",
                "family": "openai",
                "status": "exhausted",
                "meters": [
                    {
                        "label": "codex",
                        "used_pct": 100.0,
                        "reset_in": "2h",
                        "resets_at": 1_760_003_600,
                        "window_mins": 10080,
                    }
                ],
                "reason": "codex meter at 100%",
            }
        ]
    }
    row = routes._build_subscriptions([], lanes_view, 1_760_000_000.0)[0]
    assert row["status"] == "exhausted"
    assert row["pace"] is None
    assert row["eta"] == ""
    assert row["pace_warn"] is False


def test_retry_after_renders_as_a_duration_not_raw_seconds():
    """`148806` in a retry-after column is 41h the operator has to divide out."""
    from anthropic_throttle_proxy.ui import routes

    assert routes._retry_after_text({"retry-after": "148806"}) == "1d 17h"
    # Whole minutes, matching _fmt_duration everywhere else on the page.
    assert routes._retry_after_text({"retry-after": 90}) == "1m"
    assert routes._retry_after_text({}) == ""
    assert routes._retry_after_text(None) == ""
    # Never swallow a value it cannot parse — show it rather than blank it.
    assert routes._retry_after_text({"retry-after": "soon"}) == "soon"


def test_pi_registry_strip_names_every_configured_provider_with_icons():
    html = _render_stats(
        lanes={
            "registry": [
                {"icon": "✳️", "provider": "Claude"},
                {"icon": "🌀", "provider": "Codex"},
                {"icon": "✨", "provider": "Z.AI"},
                {"icon": "🚀", "provider": "Groq"},
                {"icon": "🌙", "provider": "DeepInfra"},
            ]
        }
    )
    assert "Registered providers" in html
    assert "catalog membership, not current eligibility" in html
    for text in ("✳️", "Claude", "🌀", "Codex", "✨", "Z.AI", "🚀", "Groq", "🌙", "DeepInfra"):
        assert text in html
    # Behavioral, not tautological (review minor): an id the icon table does
    # not know still renders — with the fallback icon — and the strip keeps
    # registration separate from eligibility even for unknown providers.
    html_unknown = _render_stats(lanes={"registry": [{"provider": "MysteryLane"}]})
    # Unknown providers still render (registration is not eligibility — the
    # strip's disclaimer must hold for ids the icon table has never seen).
    assert "MysteryLane" in html_unknown
    assert "same registry drives routing + meters" not in html


def test_zai_row_renders_accessible_identity_billing_and_hard_resets():
    row = {
        "id": "zai:plan",
        "identity": "Z.AI",
        "icon": "✨",
        "sub": "",
        "family": "chinese-frontier",
        "plan": "Pro V3",
        "src": "Pi meter report",
        "meters": [
            {"label": "5h", "pct": 1, "reset_in": "1h 52m"},
            {"label": "7d", "pct": 2, "reset_in": "5d 05h"},
        ],
        "pace": 0.4,
        "pace_warn": False,
        "eta": "",
        "status": "ok",
        "detail": "",
        "billing": {
            "current": True,
            "auto_renew": True,
            "cycle": "monthly",
            "renewal_amount": 80.0,
            "renewal_label": "$80/mo",
            "currency": "USD",
            "next_renew_date": "2026-09-27",
            "next_renew_display": "27/09",
            "payment_type": "WAIT_PAY",
        },
    }
    html = _render_subscription(row)
    for text in (
        "✨",
        "Z.AI",
        "billing current",
        "$80/mo",
        "renews 27/09",
        "resets 1h 52m",
        "resets 5d 05h",
    ):
        assert text in html
    for forbidden in ("customerId", "agreementNo", "orderNo", "Authorization: Bearer"):
        assert forbidden not in html


def test_zai_billing_warning_never_calls_wait_pay_a_success():
    row = {
        "id": "zai:plan",
        "identity": "Z.AI",
        "icon": "✨",
        "sub": "",
        "family": "chinese-frontier",
        "plan": "Pro V3",
        "src": "Pi meter report",
        "meters": [],
        "pace": None,
        "pace_warn": False,
        "eta": "",
        "status": "refused",
        "detail": "PAST_DUE",
        "billing": {
            "current": False,
            "auto_renew": True,
            "cycle": "monthly",
            "renewal_amount": 80.0,
            "renewal_label": "$80/mo",
            "currency": "USD",
            "next_renew_date": "2026-09-27",
            "next_renew_display": "27/09",
            "payment_type": "WAIT_PAY",
        },
    }
    html = _render_subscription(row)
    assert "billing not current" in html
    assert "payment successful" not in html
    assert "WAIT_PAY" in html

    row["billing"]["current"] = None
    stale_html = _render_subscription(row)
    assert "billing reading stale" in stale_html
    assert "billing current" not in stale_html
    assert "billing not current" not in stale_html


def test_a_rejected_window_still_says_when_it_reopens():
    """The one row where "when does it come back" is the ONLY question.

    `{% if rejected %}…{% elif reset_in %}` made the state tag and the countdown
    mutually exclusive, so a rejected meter rendered `rejected` and nothing
    else — while `reset_in` sat populated in the same dict. Live on 09/08 the
    7d row read `100% rejected` with the reset (5.5 h away) nowhere on screen.
    """
    html = _render_meter({"label": "7d", "pct": 100, "rejected": True, "reset_in": "5h 30m"})
    assert "rejected" in html
    assert "5h 30m" in html, "a rejected window must still show its reopen time"
    assert "reopens in 5h 30m" in html


def test_a_healthy_window_keeps_the_plain_resets_wording():
    html = _render_meter({"label": "5h", "pct": 12, "rejected": False, "reset_in": "1h 04m"})
    assert "resets 1h 04m" in html
    assert "reopens" not in html


def test_a_spent_meter_shows_both_its_tag_and_its_reopen():
    html = _render_meter({"label": "codex", "pct": 100, "exhausted_ok": True, "reset_in": "2d 5h"})
    assert "spent" in html
    assert "reopens in 2d 5h" in html
