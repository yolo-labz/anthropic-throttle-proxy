"""The dashboard against the 13/11/2026 07:04 health snapshot (row ui-closed-account).

At 07:04 the gmail account (``666a53af``) had been closed since 11/11 18:16,
its limiter holding Retry-After until 13/11 18:00, with nine requests queued
and one in flight on it. Its only reading was 36 h 45 min old and still said
``0.99 allowed_warning``. The proton account (``b144f62f``) had just reset and was
serving. Before this change the page said "resets in 10h 56m" for gmail and
nothing anywhere said *closed*.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import accounts
from anthropic_throttle_proxy.ui import closure, routes
from anthropic_throttle_proxy.ui.routes import attach_ui

SNAPSHOT = Path(__file__).with_name("fixtures") / "health-20261113-0704.json"
NOW = 1794564243.0  # 13/11/2026 07:04:03 BRT, just after the snapshot's newest reading
GMAIL, PROTON = "666a53af", "b144f62f"


class _Limiter:
    """Replays a limiter snapshot from health; the view only calls ``snapshot()``."""

    def __init__(self, snap: dict) -> None:
        self._snap = snap

    def snapshot(self) -> dict:
        return self._snap


@pytest.fixture
async def page(monkeypatch) -> str:
    health = json.loads(SNAPSHOT.read_text())
    state, limiters = {}, {}
    for bid, raw in health["bearers"].items():
        bearer = dict(raw)
        lim = bearer.pop("limiter", None)
        state[bid] = bearer
        if lim:
            limiters[bid] = _Limiter(lim)
    monkeypatch.setattr(routes._proxy, "bearer_state", state)
    monkeypatch.setattr(routes._proxy, "bearer_limiters", limiters)
    monkeypatch.setattr(routes.time, "time", lambda: NOW)
    monkeypatch.setattr(accounts.time, "time", lambda: NOW)
    monkeypatch.setattr(accounts, "bearer_labels", lambda: {GMAIL: "gmail", PROTON: "proton"})
    monkeypatch.setattr(
        accounts,
        "account_snapshot",
        lambda: [
            {"label": label, "path": f"/fixture/{label}", "bearer_id": bid}
            | {"expires_at": None, "error": None}
            for label, bid in (("gmail", GMAIL), ("proton", PROTON))
        ],
    )
    monkeypatch.setattr(accounts, "guard_email", lambda _path: (None, False))
    monkeypatch.setattr(routes, "_publish_account_gauges", lambda *_args: None)
    monkeypatch.setattr(routes, "_cached_fleet", lambda _now: [])
    monkeypatch.setattr(routes, "_cached_copilot", lambda _now: [])
    monkeypatch.setattr(
        routes._lanes, "view", lambda _now: {"lanes": [], "registry": [], "age_s": None}
    )
    app = web.Application()
    attach_ui(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/ui/stats?source=local")
        assert response.status == 200
        return await response.text()
    finally:
        await client.close()


def _row(html: str, label: str) -> str:
    match = re.search(rf'<tr id="subscription-{label}".*?</tr>', html, re.S)
    assert match, f"no subscription row for {label}"
    return re.sub(r"\s+", " ", match.group(0))


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


async def test_closed_account_says_closed_until_and_how_many_are_parked(page):
    row = _text(_row(page, "gmail"))
    assert "🔒 closed until 13/11 18:00 (in 10 h 56 min)" in row
    assert "9 parked · 1 in flight on a closed account" in row
    # A pace or runway beside a closed account predicts what already happened.
    assert "runs out" not in row and "1.06×" not in row


async def test_old_reading_looks_old(page):
    row = _row(page, "gmail")
    assert "⚠️ not current · reading 36 h 45 min old" in _text(row)
    # The source-clock caption is kept verbatim (test_dashboard_enrollment pins it).
    assert re.search(r"Source sample \d+s old · 11/11/2026 21:18 UTC", _text(row))
    assert 'class="sub-meters is-stale"' in row


async def test_status_strip_names_the_closure_not_a_reset(page):
    strip = _text(page.split('id="bearers-heading"')[0].split("binding constraint", 1)[1][:1500])
    assert "closed until 13/11 18:00 (in 10 h 56 min)" in strip
    assert "9 parked on it" in strip
    assert "resets in" not in strip.split("takes traffic next")[0]


async def test_open_account_reads_open(page):
    row = _text(_row(page, "proton"))
    assert "open" in row.split()
    assert "closed until" not in row
    assert "11 parked · 4 in flight" in row
    assert "on a closed account" not in row


async def test_per_token_retry_column_shows_the_closure(page):
    table = _text(page.split('id="bearers-heading"', 1)[1])
    assert f"{GMAIL} gmail 1 /1 hard 20 9 closed until 13/11 18:00" in table


@pytest.mark.parametrize(
    ("limiter", "unified", "expected"),
    [
        ({"retry_after_until": NOW + 30}, None, None),  # cooldown blip, not a closure
        ({"retry_after_until": NOW - 5}, None, None),
        ({"retry_after_until": 1794603599.5}, None, ("13/11 18:00", "10 h 56 min")),
        ({}, {"status": "rejected", "reset": NOW + 7200}, ("13/11 09:04", "2 h 0 min")),
        ({}, {"status": "rejected", "reset": NOW - 1}, None),
        ({}, {"status": "allowed_warning", "reset": NOW + 7200}, None),
        (None, None, None),
    ],
)
def test_closed_until(limiter, unified, expected):
    got = closure.closed_until(limiter, unified, NOW)
    assert (got and (got["at"], got["in"])) == expected or got == expected


@pytest.mark.parametrize(
    ("age", "text"), [(132320.6, "36 h 45 min"), (59, "1 min"), (None, None), (True, None)]
)
def test_age_text(age, text):
    assert closure.age_text(age) == text
