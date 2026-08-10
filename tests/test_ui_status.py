"""Status-strip binding-window labelling + stale-window handling.

Regression for the 19/07/2026 dashboard bug: the THROTTLED banner hardcoded
"5h window" and read a reset (stale) 5h utilisation, so a bearer whose binding
window was the 7d (representative_claim=seven_day, 87%) with an already-reset 5h
(86%) rendered as "binding: 5h window 86%" — contradicting the accounts panel
that correctly showed the 5h window as "0% · reset".
"""

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
    assert status["binding"] == {
        "bearer_id": "b144f62f",
        "window": "7d",
        "pct": 87,
        "retry_after": None,
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
        routes._provider_label("http://127.0.0.1:8766") == "127"
    )  # ip → first octet, still renders
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
    assert p["ok"] is True and p["egress_ok"] is True  # direct upstream → egress ok
    assert p["served"] == 23550 and p["max_concurrent"] == 5
    assert p["level"] == "throttled"


def test_build_providers_central_mode_reflects_central_health():
    up = _providers(central_url="http://central:9000", central_status="up")[0]
    assert up["name"] == "central" and up["upstream"] == "http://central:9000"
    assert up["egress_ok"] is True
    down = _providers(central_url="http://central:9000", central_status="down")[0]
    assert down["egress_ok"] is False  # central down → primary egress impaired


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


def _render_meter(meter: dict) -> str:
    """Render one subscription row's meters cell through the real template."""
    import jinja2

    from anthropic_throttle_proxy.ui import routes

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(routes._TEMPLATES)), autoescape=True
    )
    tpl = env.get_template("partials/stats.html")
    row = {
        "id": "x",
        "identity": "x",
        "sub": "",
        "family": "anthropic",
        "plan": "",
        "src": "proxy",
        "meters": [meter],
        "pace": None,
        "pace_warn": False,
        "eta": "",
        "status": "rejected" if meter.get("rejected") else "ok",
        "detail": "",
    }
    return tpl.render(
        subscriptions=[row],
        bearers=[],
        providers=[],
        signals=[],
        status=None,
        lanes=None,
        last_advisor=None,
        served=0,
        inflight=0,
        queued=0,
        holds=0,
        retries=0,
        disconnects=0,
    )


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
