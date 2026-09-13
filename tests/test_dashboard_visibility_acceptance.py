"""Exact-row acceptance for the user's explicit visibility/icon requirements."""

from html.parser import HTMLParser

import jinja2

from anthropic_throttle_proxy.ui import routes
from anthropic_throttle_proxy.ui.presentation import apply_display


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.current = dict(attrs).get("data-subscription-id")
            if self.current:
                self.rows[self.current] = ""

    def handle_endtag(self, tag):
        if tag == "tr":
            self.current = None

    def handle_data(self, data):
        if self.current:
            self.rows[self.current] += data


def test_user_hidden_provider_and_limit_icons_are_scoped_to_the_actual_rows():
    def subscription(sid, family, icon):
        return {
            "id": sid,
            "family": family,
            "icon": icon,
            "label": sid,
            "identity": sid,
            "plan": "observed-tier",
            "status": "unknown",
            "plan_caption": "operator caption",
            "plan_conflict": True,
            "meters": [
                {"label": "pool-week", "window_mins": 10080, "pct": 24},
                {"label": "pool-short", "window_mins": 300, "pct": 5},
            ],
            "pace": None,
            "eta": "",
            "detail": "model applicability unknown",
        }

    view = {
        "subscriptions": [
            subscription("codex:c", "openai", "🅒"),
            subscription("zai:plan", "chinese-frontier", "⚡"),
            subscription("canceled-account", "anthropic", "✳️"),
        ],
        "providers": [{"name": "127", "kind": "primary"}],
        "bearers": [{"bearer_id": "synthetic"}],
        "lanes": {
            "registry": [{"provider": "Claude"}, {"provider": "Codex"}],
            "age_s": 30,
            "interval_s": 900,
        },
        "status": {"level": "healthy", "verdict": "HEALTHY"},
        "signals": [],
        "served": 0,
        "inflight": 0,
        "queued": 0,
        "upstream": "http://127.0.0.1:1",
        "central_url": "(direct)",
    }
    projected = apply_display(
        view,
        {
            "defaults": {
                "hidden_families": ["anthropic"],
                "show_primary": False,
            }
        },
    )
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(routes._TEMPLATES),
        autoescape=True,
    )
    html = env.get_template("partials/stats.html").render(**projected)
    rows = Rows()
    rows.feed(html)
    assert set(rows.rows) == {"codex:c", "zai:plan"}
    for sid in rows.rows:
        assert "📅" in rows.rows[sid] and "⏱️" in rows.rows[sid]
        assert "observed-tier" in rows.rows[sid]
        assert "Configured caption: operator caption" in rows.rows[sid]
    assert "🅒" not in rows.rows["codex:c"]
    assert "HEALTHY" not in html
    assert "Claude" not in html and "canceled-account" not in html
    assert "127.0.0.1:1" not in html
    assert "Registered providers" in html
    assert view["status"]["verdict"] == "HEALTHY"  # display never rewrites admission inputs
