"""Opt-in dashboard token gate (THROTTLE_UI_TOKEN_FILE, ui/auth.py)."""

from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from anthropic_throttle_proxy import consumers as cs
from anthropic_throttle_proxy.ui import auth, cache_health, usage
from anthropic_throttle_proxy.ui.routes import attach_ui

FIXTURE_KEY = "fixture-ui-token-7f3a"
REMOTE = "203.0.113.7"  # TEST-NET-3
# htmx-style POSTs: config_set answers a refused value with 200 to htmx (400 to
# a plain form), so a 200 here means "the gate let the request reach the handler".
HX = {"HX-Request": "true"}


_REMOTE = web.AppKey("remote", bool)


@web.middleware
async def _as_remote(request, handler):
    """Test-only: make the TestClient's loopback peer look like a remote one."""
    if request.app[_REMOTE]:
        request = request.clone(remote=REMOTE)
    return await handler(request)


async def _health(_request):
    return web.json_response({"ok": True})


def _app(*, remote: bool) -> web.Application:
    app = web.Application(middlewares=[_as_remote])
    app[_REMOTE] = remote
    app.router.add_get("/__throttle/health", _health)  # mounted outside the gate, as in proxy.main
    cs.attach_consumers(app)
    attach_ui(app)
    usage.attach_usage(app)
    cache_health.attach_cache_health(app)
    app.on_startup.clear()  # no account refresher / history sampler in unit tests
    return app


@pytest.fixture
def gated(tmp_path, monkeypatch):
    path = tmp_path / "ui-token"
    path.write_text(FIXTURE_KEY + "\n")
    monkeypatch.setenv(auth.ENV, str(path))
    monkeypatch.setenv(cs.ENV, "1")
    auth._load.cache_clear()
    yield
    auth._load.cache_clear()


async def _client(remote: bool) -> TestClient:
    client = TestClient(TestServer(_app(remote=remote)))
    await client.start_server()
    return client


async def test_loopback_unchanged_when_gate_on(gated):
    client = await _client(remote=False)
    try:
        assert (await client.get("/ui/config")).status == 200
        assert (await client.get("/__throttle/consumers")).status == 200
        # no CSRF value is demanded of, or rendered for, a loopback peer
        resp = await client.post("/ui/config", data={"key": ""}, headers=HX)
        assert resp.status == 200
        body = await resp.text()
        assert "missing" in body  # the real handler ran; nothing was mutated
        assert 'name="csrf"' not in body
    finally:
        await client.close()


async def test_gate_off_leaves_remote_open(monkeypatch):
    monkeypatch.delenv(auth.ENV, raising=False)
    client = await _client(remote=True)
    try:
        assert (await client.get("/ui/config")).status == 200
    finally:
        await client.close()


async def test_remote_denied_without_token(gated):
    client = await _client(remote=True)
    try:
        page = await client.get("/ui", allow_redirects=False)
        assert page.status == 303
        assert page.headers["Location"] == auth.LOGIN_PATH
        for path in ("/ui/config", "/ui/stats", "/__throttle/consumers", "/ui/consumers"):
            r = await client.get(path, headers={"HX-Request": "true"})
            assert r.status == 401, path
        assert (await client.post("/ui/config", data={"key": ""}, headers=HX)).status == 401
        bad = {auth.AUTH_HEADER: FIXTURE_KEY + "x"}
        assert (await client.get("/__throttle/consumers", headers=bad)).status == 401
    finally:
        await client.close()


async def test_relayed_loopback_is_remote(gated):
    client = await _client(remote=False)
    try:
        r = await client.get("/ui/config", headers={"X-Forwarded-For": REMOTE, "HX-Request": "1"})
        assert r.status == 401
    finally:
        await client.close()


async def test_remote_allowed_with_header_token(gated):
    client = await _client(remote=True)
    try:
        hdr = {auth.AUTH_HEADER: FIXTURE_KEY}
        assert (await client.get("/ui/config", headers=hdr)).status == 200
        assert (await client.get("/__throttle/consumers", headers=hdr)).status == 200
        # header auth is not ambient, so it needs no CSRF value
        posted = await client.post("/ui/config", data={"key": ""}, headers={**hdr, **HX})
        assert posted.status == 200
    finally:
        await client.close()


async def test_login_cookie_and_csrf(gated):
    client = await _client(remote=True)
    try:
        assert (await client.get(auth.LOGIN_PATH)).status == 200
        wrong = await client.post(auth.LOGIN_PATH, data={"token": "nope"}, allow_redirects=False)
        assert wrong.status == 401
        ok = await client.post(auth.LOGIN_PATH, data={"token": FIXTURE_KEY}, allow_redirects=False)
        assert ok.status == 303
        cookie = ok.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert FIXTURE_KEY not in cookie

        page = await client.get("/ui/config")
        assert page.status == 200
        html = await page.text()
        csrf = auth.current_gate().csrf_value()
        assert f'name="csrf" value="{csrf}"' in html

        # cookie alone cannot POST: that is exactly what a forged cross-site form has
        assert (await client.post("/ui/config", data={"key": ""}, headers=HX)).status == 403
        bad = {auth.CSRF_HEADER: "0" * 64, **HX}
        assert (await client.post("/ui/config", data={"key": ""}, headers=bad)).status == 403
        hdr = {auth.CSRF_HEADER: csrf, **HX}
        assert (await client.post("/ui/config", data={"key": ""}, headers=hdr)).status == 200
        field = await client.post("/ui/config", data={"key": "", "csrf": csrf}, headers=HX)
        assert field.status == 200
    finally:
        await client.close()


async def test_health_open_and_token_never_logged(gated, caplog):
    caplog.set_level(logging.DEBUG)
    client = await _client(remote=True)
    try:
        assert (await client.get("/__throttle/health")).status == 200
        await client.get("/ui/config", headers={auth.AUTH_HEADER: FIXTURE_KEY})
        await client.get("/ui/config", headers={auth.AUTH_HEADER: "wrong"})
        await client.post(auth.LOGIN_PATH, data={"token": FIXTURE_KEY}, allow_redirects=False)
        await client.post(auth.LOGIN_PATH, data={"token": "wrong"}, allow_redirects=False)
    finally:
        await client.close()
    assert "ui auth gate enabled" in caplog.text
    assert FIXTURE_KEY not in caplog.text


def test_unreadable_token_file_fails_closed(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(auth.ENV, str(tmp_path / "missing"))
    auth._load.cache_clear()
    try:
        gate = auth.current_gate()
        assert gate.enabled
        assert not gate.token_matches("")
        assert not gate.token_matches("anything")
        assert not gate.cookie_matches("anything")
    finally:
        auth._load.cache_clear()


@pytest.mark.parametrize(
    ("peer", "local"), [("127.0.0.1", True), ("::1", True), (REMOTE, False), ("", False)]
)
def test_is_local(peer, local):
    request = make_mocked_request("GET", "/ui").clone(remote=peer)
    assert auth.is_local(request) is local


async def test_consumer_fragment_and_json_are_gated_for_remote(gated):
    """The dashboard's #consumers panel polls /ui/consumers (hx-get, every 10s);
    that fragment and /__throttle/consumers must sit behind the gate remotely."""
    routes = {
        r.resource.canonical: r.handler
        for r in _app(remote=True).router.routes()
        if r.resource and r.method == "GET"
    }
    for path, inner in (
        ("/ui/consumers", cs.consumers_html),
        ("/__throttle/consumers", cs.consumers_json),
    ):
        assert getattr(routes[path], "__wrapped__", None) is inner, path

    local = await _client(remote=False)
    try:
        fragment = await (await local.get("/ui/consumers")).text()
        snapshot = await (await local.get("/__throttle/consumers")).json()
    finally:
        await local.close()

    client = await _client(remote=True)
    try:
        assert (await client.get("/ui/consumers", headers=HX)).status == 401
        assert (await client.get("/__throttle/consumers")).status == 401
        ok = await client.post(auth.LOGIN_PATH, data={"token": FIXTURE_KEY}, allow_redirects=False)
        assert ok.status == 303
        polled = await client.get("/ui/consumers", headers=HX)  # cookie rides the htmx poll
        assert polled.status == 200
        assert await polled.text() == fragment
        hdr = {auth.AUTH_HEADER: FIXTURE_KEY}
        assert (await client.get("/__throttle/consumers", headers=hdr)).status == 200
        assert (await (await client.get("/__throttle/consumers", headers=hdr)).json()).keys() == (
            snapshot.keys()
        )
    finally:
        await client.close()


async def test_usage_fragment_is_gated_for_remote(gated, tmp_path, monkeypatch):
    """#30 mounted /ui/usage after #28's gate; it must sit behind it remotely."""
    routes = {r.resource.canonical: r.handler for r in _app(remote=True).router.routes()}
    assert getattr(routes["/ui/usage"], "__wrapped__", None) is usage.usage_partial
    monkeypatch.setenv(usage.ENV, str(tmp_path / "absent.json"))
    client = await _client(remote=True)
    try:
        assert (await client.get("/ui/usage", headers=HX)).status == 401
        hdr = {auth.AUTH_HEADER: FIXTURE_KEY}
        assert (await client.get("/ui/usage", headers=hdr)).status == 200
    finally:
        await client.close()
    local = await _client(remote=False)
    try:
        assert (await local.get("/ui/usage")).status == 200
    finally:
        await local.close()


async def test_cache_fragment_is_gated_for_remote(gated, monkeypatch):
    """Spec 381: /ui/cache reports live token counters; remote needs the token."""
    routes = {r.resource.canonical: r.handler for r in _app(remote=True).router.routes()}
    assert getattr(routes["/ui/cache"], "__wrapped__", None) is cache_health.cache_health_partial
    monkeypatch.setattr(cache_health, "token_samples", lambda: [])
    client = await _client(remote=True)
    try:
        assert (await client.get("/ui/cache", headers=HX)).status == 401
        ok = await client.get("/ui/cache", headers={auth.AUTH_HEADER: FIXTURE_KEY})
        assert ok.status == 200 and "no traffic yet" in await ok.text()
    finally:
        await client.close()


def _main_attach_calls() -> list:
    """Every ``attach_*`` that ``proxy.main`` mounts, resolved in source order."""
    import importlib
    import inspect
    import re

    from anthropic_throttle_proxy import proxy

    src = inspect.getsource(proxy.main)
    where = {n: m for m, n in re.findall(r"from (\.[\w.]*) import (attach_\w+)", src)}
    out = []
    for name in re.findall(r"^\s+(attach_\w+)\(app\)", src, re.M):
        mod = where.get(name)
        pkg = importlib.import_module(mod, "anthropic_throttle_proxy") if mod else proxy
        out.append(getattr(pkg, name))
    return out


async def test_every_ui_route_refuses_an_untokened_remote(gated):
    """Tripwire: mount exactly what ``proxy.main`` mounts, then require the gate on
    every /ui route (bar login and static assets) and on the two inspection JSONs.
    A new route that forgets ``guard`` fails here instead of shipping open."""
    app = web.Application(middlewares=[_as_remote])
    app[_REMOTE] = True
    for attach in _main_attach_calls():
        attach(app)
    app.on_startup.clear()
    open_ok = (auth.LOGIN_PATH, "/ui/static/")
    guarded_api = {"/__throttle/consumers", "/__throttle/api-capacity"}
    checked = []
    for r in app.router.routes():
        path = r.resource.canonical if r.resource else ""
        is_ui = path == "/ui" or path.startswith("/ui/")
        if r.method == "HEAD" or not (is_ui or path in guarded_api):
            continue
        if path.startswith(open_ok):
            continue
        checked.append((r.method, path))
    assert {p for _, p in checked} >= {"/ui", "/ui/usage", "/ui/cache"} | guarded_api
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for method, path in checked:
            resp = await client.request(method, path, headers=HX, allow_redirects=False)
            assert resp.status in (401, 403), (method, path, resp.status)
    finally:
        await client.close()
