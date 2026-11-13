"""Spec 383: per-account quota runway from the recent 7d slope (ui/runway.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jinja2

from anthropic_throttle_proxy import proxy
from anthropic_throttle_proxy.ui import routes, runway

BRT = timezone(timedelta(hours=-3))
NOW = datetime(2026, 11, 11, 10, 35, tzinfo=BRT).timestamp()
RESET = datetime(2026, 11, 13, 7, 0, tzinfo=BRT).timestamp()
PER_H = 0.35 / 3600


def _steady(u_now: float, minutes: int = 30, reset: float = RESET) -> list:
    return [(NOW - 60 * m, u_now - PER_H * 60 * m, reset) for m in range(minutes, -1, -1)]


def test_steady_slope_gives_the_exact_time():
    r = runway.runway(_steady(0.34), NOW, stale=False)
    at = NOW + 0.66 / PER_H  # 1.8857 h after 10:35 -> 12:28:09
    assert r["state"] == "runs_out"
    assert abs(r["at"] - at) < 1e-6
    assert r["text"] == "runs out at 11/11 12:28 (in 1 h 53 min)"


def test_reset_discontinuity_uses_only_the_later_samples():
    # 0.95 before a reset 20 min ago; afterwards the same 0.35/h.
    before = [(NOW - 60 * m, 0.95, RESET - 7 * 86400) for m in range(40, 20, -1)]
    after = _steady(0.34, minutes=20)
    r = runway.runway(before + after, NOW, stale=False)
    assert r["state"] == "runs_out"
    assert abs(r["at"] - (NOW + 0.66 / PER_H)) < 1e-6
    # A plain decrease (recalibration) with the same reset epoch also cuts.
    dropped = [(NOW - 60 * m, 0.90, RESET) for m in range(40, 20, -1)] + after
    assert abs(runway.runway(dropped, NOW, stale=False)["at"] - (NOW + 0.66 / PER_H)) < 1e-6


def test_flat_meter_is_a_dash():
    flat = [(NOW - 60 * m, 0.34, RESET) for m in range(30, -1, -1)]
    r = runway.runway(flat, NOW, stale=False)
    assert (r["state"], r["text"]) == ("none", "—")
    assert "too few readings" in r["reason"]  # one distinct value
    # Three distinct values but a plateau: the median pairwise slope is 0.
    plateau = [
        (NOW - 60 * m, 0.34 + 0.01 * (m < 2) + 0.01 * (m < 1), RESET) for m in range(30, -1, -1)
    ]
    assert runway.runway(plateau, NOW, stale=False)["reason"] == "meter not rising"


def test_stale_reading_is_a_dash():
    r = runway.runway(_steady(0.34), NOW, stale=True)
    assert (r["state"], r["text"], r["reason"]) == ("none", "—", "quota reading is stale")


def test_too_few_points_is_a_dash():
    r = runway.runway(_steady(0.34, minutes=5), NOW, stale=False)  # 5 min span
    assert r["state"] == "none" and "too few readings" in r["reason"]
    assert runway.runway([], NOW, stale=False)["state"] == "none"


def test_runway_past_the_reset_lasts_to_reset():
    soon = NOW + 3600  # reset in 1 h; 0.66 left at 0.35/h needs 1.9 h
    r = runway.runway(_steady(0.34, reset=soon), NOW, stale=False)
    assert (r["state"], r["text"]) == ("lasts", "lasts to reset")


def test_one_outlier_does_not_move_the_robust_slope():
    pts = _steady(0.34)
    pts[10] = (pts[10][0], pts[10][1] + 0.2, RESET)  # a spike, then back on the line
    assert abs(runway.runway(pts, NOW, stale=False)["at"] - (NOW + 0.66 / PER_H)) < 1.0


def test_subscription_row_reads_the_week_samples(monkeypatch):
    series = _steady(0.34)
    monkeypatch.setattr(proxy._WEEK_SAMPLES, "samples", lambda key: series if key == "b1" else [])
    monkeypatch.setattr(routes.time, "time", lambda: NOW)
    row = routes._anthropic_subscription_row({"label": "A", "bearer_id": "b1", "pace": 0.54})
    assert row["runway"]["text"] == "runs out at 11/11 12:28 (in 1 h 53 min)"
    stale = routes._anthropic_subscription_row(
        {"label": "A", "bearer_id": "b1", "pace": 0.54, "reading_stale": True}
    )
    assert stale["runway"]["reason"] == "quota reading is stale"
    unknown = routes._anthropic_subscription_row({"label": "B", "bearer_id": "b2", "pace": 0.5})
    assert unknown["runway"]["text"] == "—"


def _burn_cell(row: dict) -> str:
    src = (routes._TEMPLATES / "partials" / "stats.html").read_text()
    start = src.index('<td class="burn">')
    cell = src[start : src.index("</td>", start) + 5]
    return jinja2.Environment(autoescape=True).from_string(cell).render(s=row)


def test_burn_cell_shows_the_runway_instead_of_no_exhaustion():
    base = {"pace": 0.54, "pace_warn": False, "eta": "", "status": "ok"}
    html = _burn_cell({**base, "runway": runway.runway(_steady(0.34), NOW, stale=False)})
    assert "runs out at 11/11 12:28 (in 1 h 53 min)" in html and "is-warn" in html
    assert "no exhaustion" not in html
    dash = _burn_cell({**base, "runway": runway.runway([], NOW, stale=False)})
    assert "— · too few readings in the last 30 min" in dash
    lane = _burn_cell({**base, "runway": None})  # non-Anthropic rows keep the old ETA
    assert "no exhaustion" in lane
