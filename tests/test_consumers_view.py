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
