"""The server-rendered dashboard links to provider attribution in both modes."""

from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest


class ObservatoryLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_header = False
        self.current = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "header":
            self.in_header = True
        if tag == "a" and dict(attrs).get("href") == "/ui/observatory":
            self.current = []
            self.links.append((self.in_header, dict(attrs), self.current))

    def handle_endtag(self, tag):
        if tag == "header":
            self.in_header = False
        if tag == "a":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)


@pytest.mark.parametrize("show_local", [True, False], ids=["gateway", "meters"])
def test_dashboard_has_one_native_observatory_header_link(show_local):
    templates = Path(__file__).resolve().parents[1] / "src/anthropic_throttle_proxy/ui/templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(templates), autoescape=True)
    html = env.get_template("dashboard.html").render(
        show_local=show_local,
        central_url="(direct)",
        asset_v="fixture",
    )
    assert ("direct upstream" if show_local else "read-only account meters") in html
    parsed = ObservatoryLinks()
    parsed.feed(html)
    assert len(parsed.links) == 1, "expected one /ui/observatory link"
    in_header, attrs, text = parsed.links[0]
    assert in_header
    assert " ".join("".join(text).split()) == "Provider Observatory"
    assert {"hidden", "aria-hidden", "aria-label", "role", "target", "tabindex"}.isdisjoint(attrs)
