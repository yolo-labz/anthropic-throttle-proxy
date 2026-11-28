"""Structural checks for the Observatory legend and native skip link."""

import json
from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest

from anthropic_throttle_proxy.ui import observatory, usage

ROOT = Path(__file__).resolve().parents[1]


class Markup(HTMLParser):
    # Keep tokens in order so the checks can inspect visible text as well as attributes.
    def __init__(self, html):
        super().__init__()
        self.tokens = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tokens.append(("start", tag, dict(attrs)))

    def handle_endtag(self, tag):
        self.tokens.append(("end", tag, None))

    def handle_data(self, data):
        self.tokens.append(("text", data, None))

    def by_class(self, name):
        refs = [
            (index, tag, attrs)
            for index, (kind, tag, attrs) in enumerate(self.tokens)
            if kind == "start" and name in attrs.get("class", "").split()
        ]
        assert len(refs) == 1
        start, tag, attrs = refs[0]
        end = self.tokens.index(("end", tag, None), start + 1)
        text = " ".join(v.strip() for kind, v, _ in self.tokens[start + 1 : end] if kind == "text")
        return tag, attrs, text.strip()

    def by_id(self, name):
        return [
            tag for kind, tag, attrs in self.tokens if kind == "start" and attrs.get("id") == name
        ]


@pytest.fixture
def render(tmp_path, monkeypatch):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "src/anthropic_throttle_proxy/ui/templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    template = env.get_template("observatory.html")
    doc = json.loads((ROOT / "specs/380-usage-panel/usage-summary.v1.example.json").read_text())

    def draw(state):
        monkeypatch.delenv(usage.ENV, raising=False)
        if state != "missing":
            if state == "unsupported":
                doc["schema"] = "unsupported-test-schema"
            path = tmp_path / "usage.json"
            path.write_text(json.dumps(doc))
            monkeypatch.setenv(usage.ENV, str(path))
        view = observatory.view({}, now=1795831200)
        return template.render(
            ob=view,
            roles=observatory.ROLES,
            measures=observatory.MEASURES,
            columns=observatory.COLUMNS,
            asset_v="fixture",
        ), view

    return draw


def test_chart_legend_is_a_named_group(render):
    html, view = render("accepted")
    page = Markup(html)
    _, attrs, text = page.by_class("ob-legend")
    assert attrs.get("role") == "group", "A generic div cannot expose the legend's name"
    assert attrs.get("aria-label") == "Chart legend"
    assert text == " ".join(label for _, label in view["legend"])
    assert page.by_id("ob-volume-title") == ["p"]
    assert page.by_id("ob-exact-data") == ["details"]


@pytest.mark.parametrize("state", ["accepted", "unsupported", "missing"])
def test_skip_link_names_the_main_target_in_every_state(render, state):
    html, view = render(state)
    page = Markup(html)
    tag, attrs, text = page.by_class("ob-skip")
    assert tag == "a" and attrs["href"] == "#ob-main"
    assert page.by_id("ob-main") == ["main"]
    assert attrs.get("aria-label", text) == "Skip to main content"
    if state == "accepted":
        assert view["state"] == "ok" and page.by_id("ob-volume-title") == ["p"]
        assert not page.by_id("ob-missing-title")
    else:
        assert view["state"] == ("missing" if state == "missing" else "error")
        assert page.by_id("ob-missing-title") == ["h2"]
        assert not page.by_id("ob-volume-title") and 'aria-label="Chart legend"' not in html
