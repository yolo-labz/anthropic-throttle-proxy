"""Verify native Observatory navigation through the real SSR and static mounts."""

import json
from collections import Counter
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anthropic_throttle_proxy.ui import auth, routes, usage

OBSERVATORY = "/ui/observatory"


class Shell(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.links = []
        self.in_nav = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, raw_attrs):
        attrs = dict(raw_attrs)
        self.tags.append((tag, attrs))
        if tag == "nav":
            self.in_nav = attrs.get("aria-label") == "Observatory"
        elif tag == "a" and self.in_nav:
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_nav = False

    def matching(self, key, value):
        return [(tag, attrs) for tag, attrs in self.tags if attrs.get(key) == value]


@pytest.fixture
async def ssr(tmp_path, monkeypatch):
    path = tmp_path / "summary.json"
    monkeypatch.delenv(auth.ENV, raising=False)
    monkeypatch.setenv(usage.ENV, str(path))
    monkeypatch.setattr(usage, "_cache", {"key": None, "result": (None, None)})
    app = web.Application()
    routes.attach_ui(app)
    app.on_startup.clear()  # do not start account refresh or history samplers

    def publish(count, schema="usage-summary.v1"):
        raw = json.dumps(
            {
                "schema": schema,
                "generated_at": "2026-01-01T12:05:00Z",
                "coverage": {
                    "window_start": "2026-01-01T00:00:00Z",
                    "window_end": "2026-01-01T12:00:00Z",
                    "files_scanned": 1,
                    "files_skipped": 0,
                },
                "rows": [
                    {
                        "provider": "example",
                        "model": "example",
                        "role": "turn",
                        "pane": None,
                        "requests": count,
                        "fresh_input": 0,
                        "cache_write": 0,
                        "cache_read": 0,
                        "output": 0,
                        "reasoning": 0,
                    }
                ],
            }
        ).encode()
        next_path = path.with_suffix(".next")
        next_path.write_bytes(raw)
        next_path.replace(path)
        return sha256(raw).hexdigest()

    async with TestClient(TestServer(app)) as client:
        yield client, publish


@pytest.mark.parametrize("state", ["current", "frozen", "empty", "missing", "unsupported"])
async def test_full_page_navigation_only_exposes_present_fragments(ssr, state):
    client, publish = ssr
    url = OBSERVATORY + "?provider=example&measure=requests"
    supported = state not in {"missing", "unsupported"}
    if state != "missing":
        digest = publish(7, "unrecognized" if state == "unsupported" else "usage-summary.v1")
    if state in {"frozen", "empty"}:
        url += f"&mode=frozen&as_of={digest}"
    if state == "empty":
        url += "&role=compaction"
    # Even an HX header does not turn this endpoint into a fragment consumer.
    response = await client.get(url, headers={"HX-Request": "true"})
    assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
    html = await response.text()
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    page = Shell(html)
    assert len(page.matching("aria-label", "Observatory")) == 1
    assert len(page.matching("class", "ob-skip")) == 1 and "Skip to main content" in html
    assert page.matching("id", "ob-main")[0][0] == "main"
    ids = Counter(attrs["id"] for _, attrs in page.tags if "id" in attrs)
    expected = ["/ui", "#ob-volume-title", "#ob-exact-data", "#ob-source"]
    assert page.links == (expected if supported else ["/ui", "#ob-source"])
    assert not any(key.startswith("hx-") for _, attrs in page.tags for key in attrs)
    for href in page.links[1:]:
        parsed = urlsplit(urljoin(url, href))
        assert ids[parsed.fragment] == 1
        assert parsed.query == urlsplit(url).query
    if supported:
        assert page.matching("aria-label", "Chart legend")[0][1]["role"] == "group"
        assert page.matching("id", "ob-exact-data")[0][0] == "details"
    else:
        assert not ids["ob-volume-title"] and not ids["ob-exact-data"]
        assert "Required source</a>" in html
    if state == "empty":
        assert "No rows match this scope." in html
    if state == "frozen":
        assert "Frozen snapshot" in html and "Page captured" not in html


async def test_invalid_and_expired_pins_never_render_a_shell_or_current_bytes(ssr):
    client, publish = ssr
    old = publish(7)
    publish(23)
    for pin, status in (("bad", 400), (old, 404)):
        response = await client.get(f"{OBSERVATORY}?mode=frozen&as_of={pin}")
        html = await response.text()
        assert response.status == status and response.headers["Cache-Control"] == "no-store"
        assert response.content_type == "text/plain" and "<nav" not in html
        if status == 404:
            assert html == "Pinned Observatory summary unavailable or expired."


async def test_shell_stylesheet_is_versioned_and_available_on_existing_static_mount(ssr):
    client, _ = ssr
    response = await client.get(OBSERVATORY)
    page = Shell(await response.text())
    links = [a["href"] for tag, a in page.tags if tag == "link" and a.get("rel") == "stylesheet"]
    assert links[-2:] == [
        f"/ui/static/observatory.css?v={routes._ASSET_V}",
        f"/ui/static/observatory-shell.css?v={routes._ASSET_V}",
    ]
    style = await client.get(links[-1])
    assert style.status == 200 and style.content_type == "text/css"
    source = routes._STATIC / "observatory-shell.css"
    assert await style.read() == source.read_bytes()
