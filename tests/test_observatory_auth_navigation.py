"""The existing dashboard login session also covers the Observatory journey."""

from html.parser import HTMLParser
from pathlib import Path
from secrets import token_urlsafe

from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application

from anthropic_throttle_proxy.ui import usage
from anthropic_throttle_proxy.ui.routes import attach_ui

_SAMPLE = (
    Path(__file__).resolve().parents[1] / "specs/380-usage-panel/usage-summary.v1.example.json"
)


class _Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def attrs(self, name):
        return (attrs for tag, attrs in self.tags if tag == name)


async def test_remote_dashboard_login_opens_observatory_assets_and_filtered_view(
    tmp_path, monkeypatch
):
    token = token_urlsafe()
    token_file = tmp_path / "ui-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setenv("THROTTLE_UI_TOKEN_FILE", str(token_file))
    monkeypatch.setenv(usage.ENV, str(_SAMPLE))

    app = Application()
    attach_ui(app)
    app.on_startup.clear()  # no polling or samplers in this test
    async with TestClient(TestServer(app), headers={"X-Forwarded-For": "203.0.113.9"}) as client:
        for path in ("/ui", "/ui/observatory"):
            denied = await client.get(path, allow_redirects=False)
            assert (denied.status, denied.headers.get("Location")) == (303, "/ui/login")

        login = await client.get("/ui/login", allow_redirects=False)
        assert login.status == 200
        [login_form] = _Markup(await login.text()).attrs("form")
        assert (login_form["method"], login_form["action"]) == ("post", "/ui/login")
        signed_in = await client.post(
            login_form["action"], data={"token": token}, allow_redirects=False
        )
        assert (signed_in.status, signed_in.headers.get("Location")) == (303, "/ui")

        dashboard = await client.get(signed_in.headers["Location"], allow_redirects=False)
        assert dashboard.status == 200  # cookie jar only: no auth header or query token
        [entry] = (
            a["href"]
            for a in _Markup(await dashboard.text()).attrs("a")
            if a.get("href") == "/ui/observatory"
        )
        observatory = await client.get(entry, allow_redirects=False)
        assert observatory.status == 200
        markup = _Markup(await observatory.text())
        [css_href] = (
            link["href"]
            for link in markup.attrs("link")
            if link.get("href", "").partition("?")[0] == "/ui/static/observatory.css"
        )
        assert css_href.startswith("/ui/static/observatory.css?")
        css = await client.get(css_href, allow_redirects=False)
        assert (css.status, css.content_type) == (200, "text/css")
        assert ".ob-controls" in await css.text()

        [filters] = (form for form in markup.attrs("form") if form.get("method") == "get")
        assert filters["action"] == entry
        params = {"provider": "zai"}
        filtered = await client.get(filters["action"], params=params, allow_redirects=False)
        assert filtered.status == 200
        html = await filtered.text()
        assert "2 of 6 accepted source rows" in html
        assert token not in html
        back_to_dashboard = next(
            a["href"] for a in _Markup(html).attrs("a") if a.get("href") == "/ui"
        )
        assert (await client.get(back_to_dashboard, allow_redirects=False)).status == 200

        client.session.cookie_jar.clear()
        signed_out = await client.get(entry, params=params, allow_redirects=False)
        assert (signed_out.status, signed_out.headers.get("Location")) == (303, "/ui/login")
