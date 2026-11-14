"""Opt-in token gate for the dashboard and the consumer JSON API.

Before this module the dashboard had no authentication at all: anyone who could
reach the port could read the per-account view and hot-tune live knobs through
``POST /ui/config`` — on the central Dokku instance, that is anyone on the
network. ``THROTTLE_UI_TOKEN_FILE`` turns the gate on:

* a loopback peer (no forwarding header) behaves exactly as before;
* any other peer needs the token, either in ``X-Throttle-UI-Token`` or as the
  HttpOnly ``SameSite=Strict`` cookie the ``/ui/login`` form sets;
* a cookie-authenticated POST must also carry the CSRF value (``X-Throttle-CSRF``
  header — sent by htmx via ``hx-headers`` — or a ``csrf`` form field).

The gate wraps individual UI handlers, never the app: ``/__throttle/health``,
``/metrics`` and the proxy hot path do not pass through it (invariant #4).
The token is never logged. The cookie and the CSRF value are HMACs of it, so
neither reveals it, and every comparison is ``hmac.compare_digest``.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import html
import ipaddress
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp_jinja2
from aiohttp import web

ENV = "THROTTLE_UI_TOKEN_FILE"
AUTH_HEADER = "X-Throttle-UI-Token"
CSRF_HEADER = "X-Throttle-CSRF"
COOKIE = "throttle_ui_session"
LOGIN_PATH = "/ui/login"
# A request relayed by a reverse proxy on the same host arrives from loopback;
# without this check every Dokku/nginx/caddy front door would be "local".
_FORWARDING_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")

log = logging.getLogger(__name__)
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class Gate:
    """The loaded gate. ``token is None`` with ``enabled`` means fail closed."""

    def __init__(self, enabled: bool, token: bytes | None) -> None:
        self.enabled = enabled
        self._token = token

    def _derive(self, purpose: bytes) -> str:
        assert self._token is not None
        return hmac.new(self._token, purpose, hashlib.sha256).hexdigest()

    def session_value(self) -> str:
        return self._derive(b"throttle-ui-session")

    def csrf_value(self) -> str:
        return self._derive(b"throttle-ui-csrf")

    def token_matches(self, presented: str | None) -> bool:
        if self._token is None or not presented:
            return False
        return hmac.compare_digest(presented.encode(), self._token)

    def cookie_matches(self, presented: str | None) -> bool:
        if self._token is None or not presented:
            return False
        return hmac.compare_digest(presented, self.session_value())

    def csrf_matches(self, presented: str | None) -> bool:
        if self._token is None or not presented:
            return False
        return hmac.compare_digest(presented, self.csrf_value())


# ponytail: loaded once per path — rotating the token needs a restart. Upgrade
# path: key the cache on the file's mtime if hot rotation is ever needed.
@functools.lru_cache(maxsize=4)
def _load(path: str) -> Gate:
    try:
        token = Path(path).read_text(encoding="utf-8").strip().encode()
    except OSError as exc:
        log.error("%s=%s unreadable (%s); remote /ui is denied", ENV, path, type(exc).__name__)
        return Gate(enabled=True, token=None)
    if not token:
        log.error("%s=%s is empty; remote /ui is denied", ENV, path)
        return Gate(enabled=True, token=None)
    log.info("ui auth gate enabled for non-loopback /ui and /__throttle/consumers")
    return Gate(enabled=True, token=token)


def current_gate() -> Gate:
    path = os.environ.get(ENV, "").strip()
    return _load(path) if path else Gate(enabled=False, token=None)


def is_local(request: web.Request) -> bool:
    """A direct loopback peer. Unparseable, unix-socket or relayed → not local."""
    if any(h in request.headers for h in _FORWARDING_HEADERS):
        return False
    try:
        return ipaddress.ip_address(request.remote or "").is_loopback
    except ValueError:
        return False


def _is_secure(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


async def _csrf_ok(request: web.Request, gate: Gate) -> bool:
    if gate.csrf_matches(request.headers.get(CSRF_HEADER)):
        return True
    form = await request.post()  # cached by aiohttp, so the handler still reads it
    field = form.get("csrf")
    return isinstance(field, str) and gate.csrf_matches(field)


def _deny(request: web.Request) -> web.StreamResponse:
    # A browser navigating to a page is sent to sign in; an API or htmx call
    # gets a plain 401 it can act on.
    page = request.path == "/ui" or request.path.startswith("/ui/")
    if page and request.method == "GET" and "HX-Request" not in request.headers:
        return _see_other(LOGIN_PATH)
    return web.Response(status=401, text="authentication required\n")


def _see_other(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location, "Cache-Control": "no-store"})


def guard(handler: Handler) -> Handler:
    """Wrap one UI/consumer handler with the opt-in gate."""

    @functools.wraps(handler)
    async def wrapped(request: web.Request) -> web.StreamResponse:
        gate = current_gate()
        if not gate.enabled or is_local(request):
            return await handler(request)
        if gate.token_matches(request.headers.get(AUTH_HEADER)):
            return await handler(request)  # not ambient → no CSRF exposure
        if not gate.cookie_matches(request.cookies.get(COOKIE)):
            return _deny(request)
        if request.method not in ("GET", "HEAD") and not await _csrf_ok(request, gate):
            return web.Response(status=403, text="csrf check failed\n")
        request[aiohttp_jinja2.REQUEST_CONTEXT_KEY] = {"ui_csrf": gate.csrf_value()}
        return await handler(request)

    return wrapped


def _login_page(error: str = "") -> web.Response:
    note = f'<p role="alert">{html.escape(error)}</p>' if error else ""
    page = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Throttler · sign in</title><link rel="stylesheet" href="/ui/static/style.css">'
        "<main><h1>Throttler</h1>"
        f'{note}<form method="post" action="{LOGIN_PATH}">'
        '<label for="token">Dashboard token</label> '
        '<input id="token" name="token" type="password" autocomplete="current-password" required> '
        '<button type="submit">Sign in</button></form></main></html>'
    )
    return web.Response(
        status=401 if error else 200,
        text=page,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def login_form(_request: web.Request) -> web.Response:
    return _login_page()


async def login_submit(request: web.Request) -> web.StreamResponse:
    gate = current_gate()
    if not gate.enabled or is_local(request):
        return _see_other("/ui")
    field = (await request.post()).get("token")
    if not isinstance(field, str) or not gate.token_matches(field.strip()):
        return _login_page("wrong token")
    response = _see_other("/ui")
    response.set_cookie(
        COOKIE,
        gate.session_value(),
        httponly=True,
        samesite="Strict",
        secure=_is_secure(request),
        path="/",
    )
    return response


def attach_login(app: web.Application) -> None:
    app.router.add_get(LOGIN_PATH, login_form)
    app.router.add_post(LOGIN_PATH, login_submit)
