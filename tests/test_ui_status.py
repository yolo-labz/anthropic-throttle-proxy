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
    detail = routes._compute_status(bearers, "fair", NOW)["detail"]
    assert "binding: 7d window 87% on b144f62f" in detail
    assert "5h window" not in detail  # never the hardcoded/stale label


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
    detail = routes._compute_status(bearers, "fair", NOW)["detail"]
    assert "binding: 5h window 91% on aaaa1111" in detail


def test_no_binding_line_when_all_windows_stale():
    bearers = [
        _bearer(
            "bbbb2222",
            {"util_5h": 0.5, "reset_5h": PAST, "util_7d": 0.5, "reset_7d": PAST},
        )
    ]
    detail = routes._compute_status(bearers, "fair", NOW)["detail"]
    assert "binding:" not in detail


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
    assert (
        glm["ok"] is False and glm["level"] == "throttled" and glm["err"] == "sibling unreachable"
    )
    assert glm["served"] == 0  # missing numeric fields coerce to 0, never KeyError
