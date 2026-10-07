"""Spec 285 — the consolidated, truthful quota/capacity surface.

All evidence is SYNTHETIC (constructed rows and lanes, no screenshots, console
or account records). These pin the reproduction from `assignment.md` — a
reachable routing row must not read HEALTHY beside an exhausted meter, and a
separately assigned Team subscription must render beside the individual one —
plus the contract: seat counts with no fictitious totals, per-seat
used/remaining/reset, transport ≠ capacity ≠ eligibility, unknown/stale never
usable, Copilot premium exhaustion never condemning its unlimited products,
captions escaped, and the real `_collect_view` wiring.
"""

from __future__ import annotations

import re

import pytest
from ui_render import render_stats

from anthropic_throttle_proxy import fleet_ui_config
from anthropic_throttle_proxy.ui import presentation, routes

NOW = 1_760_000_000.0


def _lane(
    lane_id: str,
    status: str,
    meters: list[dict],
    *,
    kind: str = "mimo",
    identity: str = "",
    provider: str = "MiMo",
    family: str = "chinese-frontier",
    reason: str = "",
) -> dict:
    """A normalized lane row as `lanes._normalize` shapes one (schema 1)."""
    return {
        "id": lane_id,
        "kind": kind,
        "provider": provider,
        "identity": identity or provider,
        "icon": "Ⓜ️",
        "family": family,
        "status": status,
        "plan": "",
        "billing": None,
        "meters": meters,
        "reason": reason,
    }


def _window(label: str, pct: float | None, reset_in: str = "", **extra) -> dict:
    meter = {"label": label, "used_pct": pct, "reset_in": reset_in, "resets_at": None}
    meter.update(extra)
    return meter


def _rows(lanes: list[dict]) -> list[dict]:
    return routes._build_subscriptions([], {"lanes": lanes}, NOW)


def _mimo_provider() -> list[dict]:
    return routes._build_providers(
        upstream="https://api.anthropic.com",
        central_url="(direct)",
        central_status="unknown",
        level="healthy",
        inflight=0,
        queued=0,
        served=0,
        max_concurrent=2,
        fleet=[
            {
                "name": "mimo",
                "ok": True,
                "upstream": "http://127.0.0.1:8766",
                "served": 12,
                "inflight": 0,
                "queued": 0,
                "max_concurrent": 2,
                "upstream_egress_ok": True,
            }
        ],
    )


def _summary(rows: list[dict], **over) -> dict:
    view = {
        "subscriptions": rows,
        "show_local": True,
        "inflight": 3,
        "queued": 2,
        "max_concurrent": 8,
    }
    view.update(over)
    return presentation.capacity_summary(view)


# ── the reproduction (assignment.md) ────────────────────────────────────────


def test_reproduction_reachable_transport_never_reads_healthy_beside_exhausted_quota():
    """Meter EXHAUSTED + routing row HEALTHY (because DNS resolves) is the bug.

    The routing row must label transport, capacity and eligibility separately.
    The exhausted individual seat must not hide the usable Team allowance:
    the provider is limited, not entirely exhausted, and never just HEALTHY.
    """
    rows = _rows(
        [
            _lane(
                "mimo:plan",
                "exhausted",
                [_window("5h", 100.0, "40m")],
                identity="MiMo plan",
                reason="5h meter at 100% — reopens in 40m",
            ),
            _lane("mimo:team-owner", "ok", [_window("5h", 10.0, "3h")], identity="MiMo team-owner"),
        ]
    )
    providers = _mimo_provider()
    presentation.attach_provider_capacity(providers, rows)
    mimo = providers[1]
    assert mimo["ok"] is True and mimo["dns_ok"] is True  # transport really is fine
    assert mimo["capacity"]["state"] == "limited"
    assert mimo["capacity"]["label"] == "capacity limited"
    assert set(mimo["capacity"]["matched"]) == {"mimo:plan", "mimo:team-owner"}

    html = render_stats(
        subscriptions=rows,
        providers=providers,
        summary=_summary(rows),
    )
    assert ">transport ok</span>" in html
    assert ">capacity limited</span>" in html
    assert "models unverified" in html
    assert "DNS resolved" in html
    assert re.search(r">\s*healthy\s*<", html) is None, "a transport probe is not health"
    assert "HEALTHY" not in html
    assert rows[0]["status"] == "exhausted"  # the meter verdict survives the join


def test_team_subscription_renders_alongside_the_individual_one():
    """The Team seat was absent because only the individual meter was shown."""
    rows = _rows(
        [
            _lane("mimo:plan", "exhausted", [_window("5h", 100.0, "40m")], identity="MiMo plan"),
            _lane(
                "mimo:team-owner",
                "ok",
                [_window("5h", 10.0, "3h")],
                identity="MiMo team-owner",
            ),
        ]
    )
    summary = _summary(rows)
    assert summary["total"] == 2  # two seats, counted apart — no singleton collapse
    assert summary["counts"]["exhausted"] == 1 and summary["counts"]["usable"] == 1
    html = render_stats(subscriptions=rows, summary=summary)
    assert 'data-subscription-id="mimo:plan"' in html
    assert 'data-subscription-id="mimo:team-owner"' in html
    # Distinct identity, not one merged "MiMo" row.
    assert "MiMo plan" in html and "MiMo team-owner" in html


# ── FR-1/FR-2: seat counts and binding windows, never a fictitious total ────


def test_summary_counts_seats_and_never_sums_unlike_units():
    rows = _rows(
        [
            _lane("codex:a", "ok", [_window("5h", 40.0, "1h"), _window("7d", 100.0, "2d")]),
            _lane(
                "deepseek",
                "ok",
                [{"label": "balance", "used_pct": None, "note": "$15.20", "balance_total": 15.2}],
                kind="deepseek",
                provider="DeepSeek",
            ),
            _lane("zai:plan", "exhausted", [_window("5h", 130.0, "")]),
        ]
    )
    summary = _summary(rows)
    assert summary["counts"] == {
        "exhausted": 2,
        "stale": 0,
        "unknown": 0,
        "limited": 0,
        "usable": 1,
    }
    assert summary["total"] == 3
    # Every budget stays its own number: the 5h/7d percentages and the dollar
    # balance are listed per window and never added or averaged together.
    for key in ("total_pct", "used_pct", "aggregate", "budget"):
        assert key not in summary
    labels = {(w["row"], w["window"], w["pct"]) for w in summary["windows"]}
    assert labels == {("codex:a", "7d", 100.0), ("zai:plan", "5h", 130.0)}


def test_binding_windows_come_only_from_current_evidence():
    """A stale 100 % is untrusted — it cannot name a binding window."""
    rows = _rows(
        [
            _lane("codex:a", "stale", [_window("7d", 100.0, "2d")]),
            _lane("codex:b", "exhausted", [_window("7d", 100.0, "14h")]),
        ]
    )
    summary = _summary(rows)
    assert summary["counts"]["stale"] == 1 and summary["counts"]["exhausted"] == 1
    assert [w["row"] for w in summary["windows"]] == ["codex:b"]
    assert summary["windows"][0]["reset_in"] == "14h"


def test_unassigned_seats_and_unread_rows_are_never_usable():
    """A configured row with no reading is an unassigned seat: unknown."""
    cfg = {
        "subscriptions": [
            {"id": "team-seat", "label": "Unassigned team seat", "family": "chinese-frontier"}
        ],
        "defaults": {"emoji_by_family": {}},
    }
    placeholder = fleet_ui_config.decorate([], cfg)["rows"][0]
    rows = _rows([_lane("mimo:plan", "unknown", [_window("5h", None)])])
    rows.append(placeholder)
    summary = _summary(rows)
    assert presentation.row_capacity_class(placeholder) == "unknown"
    assert summary["counts"]["usable"] == 0
    assert summary["counts"]["unknown"] == 2


# ── FR-7: Copilot premium exhaustion never condemns unlimited products ──────


def test_copilot_premium_spent_does_not_condemn_unlimited_products():
    rows = _rows(
        [
            _lane(
                "copilot:personal",
                "ok",
                [
                    {
                        "label": "premium_interactions",
                        "used_pct": 100.0,
                        "exhausted_ok": True,
                        "reset_in": "",
                    },
                    {"label": "chat", "used_pct": None, "unlimited": True, "note": ""},
                    {"label": "completions", "used_pct": None, "unlimited": True, "note": ""},
                ],
                kind="copilot",
                provider="Copilot",
                family="github",
            )
        ]
    )
    assert presentation.row_capacity_class(rows[0]) == "usable"
    summary = _summary(rows)
    assert summary["counts"]["exhausted"] == 0 and summary["counts"]["usable"] == 1
    # The spent premium meter is NOT a binding window for the whole seat —
    # chat and completions keep serving.
    assert summary["windows"] == []
    providers = routes._build_providers(
        upstream="https://api.anthropic.com",
        central_url="(direct)",
        central_status="unknown",
        level="healthy",
        inflight=0,
        queued=0,
        served=0,
        max_concurrent=2,
        fleet=[{"name": "copilot", "ok": True, "upstream": "http://127.0.0.1:8766"}],
    )
    presentation.attach_provider_capacity(providers, rows)
    assert providers[1]["capacity"]["state"] == "usable"
    html = render_stats(subscriptions=rows, providers=providers, summary=summary)
    assert ">spent</span>" in html and "unlimited" in html
    assert ">capacity usable</span>" in html


# ── FR-3: per-seat used / remaining / reset ─────────────────────────────────


def test_meter_renders_used_remaining_and_reset():
    from ui_render import render_meter

    html = render_meter(
        {
            "label": "7d",
            "pct": 2,
            "reset_in": "5d 05h",
            "remaining": 1960,
            "allowance": 2000,
            "window_mins": 10080,
        }
    )
    assert "2%" in html
    assert "1960 left of 2000" in html
    assert "resets 5d 05h" in html


def test_remaining_is_absent_when_the_provider_reports_none():
    from ui_render import render_meter

    html = render_meter({"label": "5h", "pct": 12, "reset_in": "1h 04m"})
    assert "left" not in html


# ── FR-4: live load + throughput only where measured ────────────────────────


def test_live_line_shows_load_and_throughput_only_when_measured():
    rows = _rows([_lane("mimo:plan", "ok", [_window("5h", 10.0)])])
    measured = _summary(rows, tps={"seen": True, "value": 141.27})
    assert measured["live"] == {"inflight": 3, "queued": 2, "capacity": 8}
    assert measured["throughput"] == {"value": 141.3, "unit": "tokens/s"}
    unseen = _summary(rows, tps={"seen": False, "value": 0.0})
    assert unseen["throughput"] is None
    html = render_stats(subscriptions=rows, summary=unseen)
    assert "sum-tps" not in html  # unmeasured is absent, never zero-claimed
    measured_html = render_stats(subscriptions=rows, summary=measured)
    assert "141.3 tokens/s" in measured_html
    # The subscriptions-only board is not this proxy's load.
    scoped = _summary(rows, show_local=False)
    assert scoped["live"] is None and scoped["throughput"] is None


@pytest.mark.parametrize("value", [True, -1, "nan", float("inf"), 10**1000])
def test_invalid_capacity_numbers_never_become_usable(value):
    row = {"status": "ok", "meters": [{"pct": value}]}
    assert presentation.row_capacity_class(row) == "unknown"
    assert _summary([], tps={"seen": True, "value": value})["throughput"] is None


def test_spent_nonbinding_meter_alone_does_not_condemn_every_product():
    row = {"status": "ok", "meters": [{"pct": 100, "exhausted_ok": True}]}
    assert presentation.row_capacity_class(row) == "unknown"


def test_unlimited_words_are_not_capacity_evidence():
    row = {"status": "ok", "meters": [{"note": "unlimited", "unlimited": "true"}]}
    assert presentation.row_capacity_class(row) == "unknown"


# ── FR-5/FR-6: the join is per lane, and a gap is not health ────────────────


def test_provider_join_is_per_lane_and_never_per_family():
    rows = _rows(
        [
            _lane(
                "codex:a",
                "exhausted",
                [_window("5h", 100.0)],
                kind="codex",
                provider="Codex",
                family="openai",
            )
        ]
    )
    providers = routes._build_providers(
        upstream="https://api.openai.com",
        central_url="(direct)",
        central_status="unknown",
        level="healthy",
        inflight=0,
        queued=0,
        served=0,
        max_concurrent=2,
        fleet=[],
    )
    presentation.attach_provider_capacity(providers, rows)
    # `openai` the upstream and `codex:*` share a family and nothing else —
    # a family join would paint ChatGPT's exhaustion onto an unrelated row.
    assert providers[0]["name"] == "openai"
    assert providers[0]["capacity"]["state"] == "unmeasured"
    assert providers[0]["capacity"]["matched"] == []


def test_unmeasured_capacity_is_its_own_label_not_health():
    providers = _mimo_provider()
    presentation.attach_provider_capacity(providers, [])
    assert providers[1]["capacity"]["state"] == "unmeasured"
    assert providers[1]["capacity"]["label"] == "capacity unmeasured"
    html = render_stats(providers=providers, summary=_summary([]))
    assert ">capacity unmeasured</span>" in html
    assert re.search(r">\s*healthy\s*<", html) is None
    assert "models unverified" in html


def test_provider_capacity_does_not_hide_a_usable_independent_seat():
    rows = _rows(
        [
            _lane("mimo:plan", "exhausted", [_window("5h", 100.0)]),
            _lane("mimo:team-owner", "ok", [_window("5h", 10.0)]),
        ]
    )
    providers = _mimo_provider()
    presentation.attach_provider_capacity(providers, rows)
    assert providers[1]["capacity"]["state"] == "limited"
    detail = providers[1]["capacity"]["detail"]
    assert "mimo:plan exhausted" in detail and "mimo:team-owner usable" in detail


# ── FR-8: escaping, and the real wiring ─────────────────────────────────────


def test_captions_and_labels_are_escaped():
    rows = _rows(
        [
            _lane(
                "mimo:plan",
                "exhausted",
                [_window("5h", 100.0, "<b>soon</b>")],
                identity="<img src=x onerror=alert(1)>",
            )
        ]
    )
    providers = _mimo_provider()
    presentation.attach_provider_capacity(providers, rows)
    html = render_stats(subscriptions=rows, providers=providers, summary=_summary(rows))
    assert "<img src=x" not in html and "<b>soon</b>" not in html
    assert "&lt;img src=x" in html and "&lt;b&gt;soon&lt;/b&gt;" in html


async def test_collect_view_wires_summary_and_provider_capacity(monkeypatch):
    """The delivery path: `_collect_view` (projected) carries both surfaces."""
    lanes_view = {
        "lanes": [
            _lane("mimo:plan", "exhausted", [_window("5h", 100.0, "40m")]),
            _lane("mimo:team-owner", "ok", [_window("5h", 10.0, "3h")]),
        ],
        "registry": [],
        "age_s": 30,
        "stale": False,
        "interval_s": 900,
    }
    fleet_row = {
        "name": "mimo",
        "ok": True,
        "upstream": "http://127.0.0.1:8766",
        "served": 12,
        "inflight": 0,
        "queued": 0,
        "max_concurrent": 2,
        "upstream_egress_ok": True,
    }

    monkeypatch.setattr(routes._accounts, "_endpoint_cache", {})
    monkeypatch.setattr(routes._accounts, "account_view", lambda bearers, now, endpoint: [])
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda endpoint, identity: None)
    monkeypatch.setattr(routes, "_cached_fleet", lambda now: [fleet_row])
    monkeypatch.setattr(routes, "_cached_copilot", lambda now: [])
    monkeypatch.setattr(routes._lanes, "view", lambda now: lanes_view)
    monkeypatch.setattr(
        fleet_ui_config,
        "load",
        lambda: {
            "subscriptions": [],
            "defaults": {"emoji_by_family": {}, "hidden_families": [], "show_primary": True},
        },
    )
    result = await routes._collect_view()
    assert result["summary"]["counts"]["exhausted"] == 1
    assert result["summary"]["counts"]["usable"] == 1
    mimo = next(p for p in result["providers"] if p["name"] == "mimo")
    assert mimo["capacity"]["state"] == "limited"
    assert set(mimo["capacity"]["matched"]) == {"mimo:plan", "mimo:team-owner"}
    # Raw readers keep the unfiltered snapshot without display derivation.
    raw = await routes._collect_view(project=False)
    assert "summary" not in raw
