"""Digest-pinned Observatory GETs over one synthetic published aggregate."""

import json
from hashlib import sha256
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp_jinja2
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from jinja2 import FileSystemLoader, select_autoescape

from anthropic_throttle_proxy.ui import observatory as ob
from anthropic_throttle_proxy.ui import usage

FILTERS = dict(provider="fixture-provider", model="fixture-model", role="turn", measure="requests")


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    clock = SimpleNamespace(value=1767270600.0)  # 01/01/2026 12:30 UTC
    monkeypatch.setattr(ob, "time", SimpleNamespace(time=lambda: clock.value))
    monkeypatch.setenv(usage.ENV, str(path))
    monkeypatch.setattr(usage, "_cache", {"key": None, "result": (None, None)})
    app = web.Application()
    aiohttp_jinja2.setup(
        app,
        loader=FileSystemLoader(ob.__file__.rsplit("/", 1)[0] + "/templates"),
        autoescape=select_autoescape(("html",)),
    )
    actual_read = usage._read
    reads = []

    def counted_read(source):
        assert source == path
        reads.append(source)
        return actual_read(source)

    monkeypatch.setattr(usage, "_read", counted_read)

    def publish(count):
        row = dict(
            provider=FILTERS["provider"],
            model=FILTERS["model"],
            role="turn",
            pane=None,
            requests=count,
            fresh_input=13,
            cache_write=2,
            cache_read=3,
            output=5,
            reasoning=1,
        )
        raw = json.dumps(
            dict(
                schema="usage-summary.v1",
                generated_at="2026-01-01T12:05:00Z",
                coverage=dict(
                    window_start="2026-01-01T00:00:00Z",
                    window_end="2026-01-01T12:00:00Z",
                    files_scanned=2,
                    files_skipped=0,
                ),
                rows=[row, row | dict(provider="other-fixture", requests=99)],
            )
        ).encode()
        sibling = path.with_suffix(".next")
        sibling.write_bytes(raw)
        sibling.replace(path)
        return sha256(raw).hexdigest()

    async def get(params):
        url = params if isinstance(params, str) else "/ui/observatory?" + urlencode(params)
        request = make_mocked_request("GET", url, app=app)
        try:
            return await ob.index(request)
        except web.HTTPException as exc:
            return exc

    return SimpleNamespace(get=get, publish=publish, clock=clock, reads=reads, path=path)


class Navigation(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = {}
        self.controls = []
        self.form = None
        self._link = None
        self._in_form = False
        self._select = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._link = (attrs["href"], [])
        elif tag == "form":
            assert self.form is None
            self.form = attrs
            self._in_form = True
        elif tag == "input" and self._in_form and attrs.get("type") == "hidden":
            self.controls.append((attrs["name"], attrs["value"]))
        elif tag == "select" and self._in_form:
            self._select = attrs["name"]
        elif tag == "option" and self._select and "selected" in attrs:
            self.controls.append((self._select, attrs["value"]))

    def handle_data(self, data):
        if self._link is not None:
            self._link[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            href, text = self._link
            self.links["".join(text).strip()] = href
            self._link = None
        elif tag == "select":
            self._select = None
        elif tag == "form":
            self._in_form = False


def assert_selection(href, params):
    url = urlsplit(href)
    assert (url.scheme, url.netloc, url.path, url.fragment) == ("", "", "/ui/observatory", "")
    assert sorted(parse_qsl(url.query, keep_blank_values=True)) == sorted(params.items())


async def test_navigation_keeps_the_digest_until_return_to_current(fixture):
    digest = fixture.publish(7)
    pin = dict(mode="frozen", as_of=digest)
    current = await fixture.get(FILTERS)
    assert current.status == 200 and "Manual snapshot" in current.text
    freeze_href = Navigation(current.text).links["Freeze this summary"]
    assert_selection(freeze_href, FILTERS | pin)

    frozen = await fixture.get(freeze_href)
    assert frozen.status == 200 and frozen.headers["Cache-Control"] == "no-store"
    assert "Frozen snapshot" in frozen.text and "Request time not recorded" in frozen.text
    assert "No archive is kept" in frozen.text
    assert "Manual snapshot" not in frozen.text and "Page captured" not in frozen.text
    assert "Source within one hour" not in frozen.text and "Source age" not in frozen.text
    nav = Navigation(frozen.text)
    assert sorted(nav.controls) == sorted((FILTERS | pin).items())
    assert nav.form["method"] == "get" and nav.form["action"] == "/ui/observatory"
    edited = [(k, "other-fixture" if k == "provider" else v) for k, v in nav.controls]
    submitted = await fixture.get(nav.form["action"] + "?" + urlencode(edited))
    assert submitted.status == 200 and "<td>99</td>" in submitted.text and digest in submitted.text

    edited[edited.index(("provider", "other-fixture"))] = ("provider", "fixture-provider")
    edited[edited.index(("role", "turn"))] = ("role", "compaction")
    empty = await fixture.get(nav.form["action"] + "?" + urlencode(edited))
    assert empty.status == 200 and "No rows match this scope." in empty.text
    assert_selection(Navigation(empty.text).links["Return to all recorded usage"], pin)

    clear_href = nav.links["Clear filters"]
    assert_selection(clear_href, pin)
    cleared = await fixture.get(clear_href)
    assert cleared.status == 200 and "2 of 2 accepted source rows" in cleared.text
    assert "Frozen snapshot" in cleared.text and digest in cleared.text

    return_href = nav.links["Return to current"]
    assert_selection(return_href, FILTERS)
    new_digest = fixture.publish(23)
    stale_submit = await fixture.get(nav.form["action"] + "?" + urlencode(nav.controls))
    assert stale_submit.status == 404 and new_digest not in stale_submit.text
    assert stale_submit.headers["Cache-Control"] == "no-store"
    returned = await fixture.get(return_href)
    assert returned.status == 200 and new_digest in returned.text and "<td>23</td>" in returned.text


async def test_frozen_replay_is_stable_then_unavailable_after_rotation(fixture):
    a = fixture.publish(7)
    params = FILTERS | dict(mode="frozen", as_of=a)
    first = await fixture.get(params)
    assert first.status == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    for value in FILTERS.values():
        assert f'value="{value}" selected' in first.text
    assert a in first.text and "1 of 2 accepted source rows" in first.text
    assert "2026-01-01T12:00:00+00:00" in first.text
    assert "2026-01-01T12:05:00+00:00" in first.text
    assert "<td>7</td>" in first.text

    fixture.clock.value += 2 * 86400
    replay = await fixture.get(params)
    assert (replay.status, replay.text) == (200, first.text)
    # Same displayed timestamps and same filters, but B has different aggregate bytes.
    b = fixture.publish(23)
    assert a != b
    unavailable = await fixture.get(params)
    assert unavailable.status == 404
    assert unavailable.text == "Pinned Observatory summary unavailable or expired."
    assert unavailable.headers["Cache-Control"] == "no-store"
    assert b not in unavailable.text and "<td>23</td>" not in unavailable.text
    current = await fixture.get(FILTERS)
    assert current.status == 200 and b in current.text and "<td>23</td>" in current.text
    assert "Source older than one day" in current.text
    assert len(fixture.reads) == 4  # One existing loader call per request, no historical lookup.


async def test_frozen_missing_file_does_not_show_a_live_empty_state(fixture):
    a = fixture.publish(7)
    fixture.path.unlink()
    response = await fixture.get(FILTERS | dict(mode="frozen", as_of=a))
    assert (response.status, response.text) == (
        404,
        "Pinned Observatory summary unavailable or expired.",
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert len(fixture.reads) == 1


async def test_wrong_digest_does_not_disclose_current_summary(fixture):
    digest = fixture.publish(7)
    response = await fixture.get(FILTERS | dict(mode="frozen", as_of="0" * 64))
    assert response.status == 404 and response.headers["Cache-Control"] == "no-store"
    assert response.text == "Pinned Observatory summary unavailable or expired."
    assert digest not in response.text and len(fixture.reads) == 1


@pytest.mark.parametrize(
    "bad",
    [
        [("mode", "frozen"), ("as_of", "2026-01-01T12:00:00Z")],
        [("mode", "frozen"), ("as_of", "A" * 64)],
        [("mode", "live"), ("as_of", "a" * 64)],
        [("as_of", "a" * 64)],
        [("mode", "frozen"), ("mode", "frozen"), ("as_of", "a" * 64)],
        [("mode", "frozen"), ("as_of", "a" * 64), ("as_of", "a" * 64)],
        [("mode", "frozen"), ("as_of", "a" * 64), ("account", "synthetic")],
        [("mode", "frozen"), ("as_of", "a" * 64), ("provider", "conflicting")],
    ],
)
async def test_bad_frozen_selections_fail_before_reading(fixture, bad):
    response = await fixture.get([*FILTERS.items(), *bad])
    assert response.status == 400 and response.headers["Cache-Control"] == "no-store"
    assert not fixture.reads
