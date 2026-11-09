"""Spec 378 phase 1: read-only consumer view."""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy import consumers as cs
from anthropic_throttle_proxy import project_share as ps


def _share(tmp_path) -> ps.ProjectShare:
    return ps.ProjectShare(weights_path=tmp_path / "weights.json")


def _app(share: ps.ProjectShare, monkeypatch) -> web.Application:
    monkeypatch.setattr(cs.ps, "SHARE", share)
    app = web.Application()
    cs.attach_consumers(app)
    return app


def test_default_off_registers_no_routes(monkeypatch):
    monkeypatch.delenv(cs.ENV, raising=False)
    app = web.Application()
    cs.attach_consumers(app)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert "/__throttle/consumers" not in paths
    assert "/ui/consumers" not in paths


async def test_routes_are_get_only_and_writes_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(cs.ENV, "1")
    app = _app(_share(tmp_path), monkeypatch)
    methods = {
        (r.resource.canonical, r.method)
        for r in app.router.routes()
        if r.resource
        and r.resource.canonical.startswith(("/__throttle/consumers", "/ui/consumers"))
    }
    assert {p for p, _ in methods} == {"/__throttle/consumers", "/ui/consumers"}
    assert {m for _, m in methods} <= {"GET", "HEAD"}
    async with TestClient(TestServer(app)) as c:
        for path in ("/__throttle/consumers", "/ui/consumers"):
            r = await c.post(path, data={"x": "1"})
            assert r.status in (404, 405), (path, r.status)


async def test_absent_header_is_direct_row(tmp_path, monkeypatch):
    monkeypatch.setenv(cs.ENV, "1")
    share = _share(tmp_path)
    # Absent / unusable header resolves to _direct before reaching the ledger.
    assert ps.project_of({}) == ps.DIRECT
    assert ps.project_of({ps.PROJECT_HEADER: "!!!"}) == ps.DIRECT
    share.record_spend(ps.project_of({}), 1.5)
    async with TestClient(TestServer(_app(share, monkeypatch))) as c:
        body = await (await c.get("/__throttle/consumers")).json()
    rows = {r["project"]: r for r in body["consumers"]}
    assert ps.DIRECT in rows and rows[ps.DIRECT]["usd_5h"] == 1.5


async def test_empty_ledger_still_has_direct_row(tmp_path, monkeypatch):
    monkeypatch.setenv(cs.ENV, "1")
    async with TestClient(TestServer(_app(_share(tmp_path), monkeypatch))) as c:
        body = await (await c.get("/__throttle/consumers")).json()
    assert [r["project"] for r in body["consumers"]] == [ps.DIRECT]


def test_entitlement_follows_weights(tmp_path):
    share = _share(tmp_path)
    now = time.time()
    share.record_spend("alpha", 2.0, now=now)
    share.record_spend("beta", 2.0, now=now)
    share.set_weight("alpha", 4)
    snap = cs.snapshot(share, now=now)
    rows = {r["project"]: r for r in snap["consumers"]}
    assert rows["alpha"]["entitlement"] == pytest.approx(ps.USABLE * 4 / 5, abs=1e-3)
    assert rows["beta"]["entitlement"] == pytest.approx(ps.USABLE * 1 / 5, abs=1e-3)


def test_500_consumers_under_50ms(tmp_path):
    share = _share(tmp_path)
    now = time.time()
    for i in range(500):
        share.record_spend(f"proj-{i:03d}", 0.5 + i * 0.001, now=now)
    cs.snapshot(share, now=now)  # warm
    timings = []
    for _ in range(5):
        t0 = time.perf_counter()
        cs.snapshot(share, now=now)
        timings.append(time.perf_counter() - t0)
    best = min(timings)
    snap = cs.snapshot(share, now=now)
    assert len(snap["consumers"]) >= 500
    assert best < 0.050, f"snapshot took {best * 1000:.1f} ms"


# --- spec 378: per-seat observation (x-anthropic-throttle-seat) -------------


def _seat(raw: str | None) -> str:
    return cs.seat_key(ps.seat_of({} if raw is None else {ps.SEAT_HEADER: raw}))


def test_absent_or_unusable_seat_header_is_direct():
    assert _seat(None) == ps.DIRECT
    assert _seat("") == ps.DIRECT
    assert _seat(" /._- ") == ps.DIRECT


def test_spoofed_seat_cannot_claim_direct_or_inject_markup():
    assert _seat("_direct") == "direct"  # leading underscore stripped
    assert _seat("-") == ps.DIRECT  # unusable, not a claim
    assert _seat("_other") == "other"  # the overflow bucket is unclaimable too
    spoof = _seat('<script>alert("x")</script>')
    assert set(spoof) <= set("abcdefghijklmnopqrstuvwxyz0123456789._/-")
    seats = cs.SeatLedger()
    seats.record(spoof, "alpha", 1.0, now=1000.0)
    page = cs.render_fragment(cs.snapshot(ps.ProjectShare(), now=1000.0, seats=seats))
    assert "<script" not in page and "alert(" not in page or "alert-" in spoof


def test_oversize_seat_header_is_truncated_and_cardinality_bounded():
    assert len(_seat("a" * 10_000)) == 64
    seats = cs.SeatLedger(max_seats=8)
    for i in range(1000):
        seats.record(_seat(f"spoof-{i}-" + "x" * 500), "alpha", 0.01, now=1000.0)
    rows = {r["seat"]: r for r in seats.rows(now=1000.0)}
    assert len(rows) == 9 and cs.OTHER in rows  # 8 real seats + one overflow bucket
    assert rows[cs.OTHER]["turns_5h"] == 1000 - 8
    assert all(len(s) <= 64 for s in rows)


def test_seat_window_expires_and_frees_cardinality():
    seats = cs.SeatLedger(max_seats=1)
    seats.record("old", "alpha", 1.0, now=0.0)
    later = ps.WINDOW_S + 1
    seats.record("new", "alpha", 2.0, now=later)  # old seat aged out: no overflow
    assert [r["seat"] for r in seats.rows(now=later)] == ["new"]


def test_record_seat_is_default_off(monkeypatch):
    monkeypatch.delenv(cs.ENV, raising=False)
    fresh = cs.SeatLedger()
    monkeypatch.setattr(cs, "SEATS", fresh)
    cs.record_seat({ps.SEAT_HEADER: "s/w/r"}, "alpha", 1.0)
    assert fresh.rows() == []
    monkeypatch.setenv(cs.ENV, "1")
    cs.record_seat({ps.SEAT_HEADER: "s/w/r"}, None, 1.0)
    assert [(r["seat"], r["project"]) for r in fresh.rows()] == [("s/w/r", ps.DIRECT)]


def test_record_usage_feeds_seats_but_never_the_allocator(tmp_path, monkeypatch):
    import json

    from anthropic_throttle_proxy import proxy

    monkeypatch.setenv(cs.ENV, "1")
    share, seats = _share(tmp_path), cs.SeatLedger()
    monkeypatch.setattr(proxy, "_PROJECT_SHARE", share)
    monkeypatch.setattr(cs, "SEATS", seats)
    body = json.dumps(
        {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "model": "claude-opus-5-5"}
    ).encode()
    headers = {ps.SEAT_HEADER: "side-projects/w2p/wk", ps.PROJECT_HEADER: "nixos"}
    proxy._record_usage(
        "claude-opus-5-5",
        "claude-opus-5-5",
        bytearray(body),
        "v1/messages",
        project="nixos",
        seat_headers=headers,
    )
    (row,) = seats.rows()
    assert row["seat"] == "side-projects/w2p/wk" and row["project"] == "nixos"
    assert row["usd_5h"] > 0 and row["turns_5h"] == 1
    # The seat is observation only: the allocator ledger knows projects, never seats.
    assert {r.project for r in share.view().rows} == {"nixos"}


async def test_ui_fragment_has_seats_column_and_direct_row(tmp_path, monkeypatch):
    monkeypatch.setenv(cs.ENV, "1")
    share, seats = _share(tmp_path), cs.SeatLedger()
    now = time.time()
    share.record_spend("alpha", 3.0, now=now)
    seats.record("s/w/a", "alpha", 2.0, now=now)
    seats.record("s/w/b", "alpha", 1.0, now=now)
    seats.record(ps.DIRECT, ps.DIRECT, 0.0, now=now)
    monkeypatch.setattr(cs, "SEATS", seats)
    async with TestClient(TestServer(_app(share, monkeypatch))) as c:
        page = await (await c.get("/ui/consumers")).text()
        body = await (await c.get("/__throttle/consumers")).json()
    assert "<!doctype" not in page.lower()  # an HTMX fragment, not a page
    assert "seats seen" in page and "<code>s/w/a</code> <code>s/w/b</code>" in page
    assert "never an allocator key" in page
    rows = {r["project"]: r for r in body["consumers"]}
    assert rows["alpha"]["seats"] == ["s/w/a", "s/w/b"]
    assert rows[ps.DIRECT]["seats"] == [ps.DIRECT]
    assert [s["seat"] for s in body["seats"]][:2] == ["s/w/a", "s/w/b"]
