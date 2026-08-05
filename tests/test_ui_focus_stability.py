"""Every polled region keeps its own id, so focus survives the 2 s swap.

`#stats` re-renders every 2 seconds (`hx-trigger="every 2s"`). htmx restores
focus after a swap only when the focused element carries an id: measured
05/08/2026 on the live page, an operator focused on a scrollable table region
was thrown back to `<body>` within one cycle —

    before {"id":"(none)","tag":"SECTION"}
    after  {"id":"(none)","tag":"BODY","isBody":true}

With ids, the same probe keeps focus on `#providers-scroll`. A keyboard or
screen-reader user reading a table mid-incident loses their place otherwise
(WCAG 2.1 SC 3.2.5 — no unexpected change of context).
"""

from __future__ import annotations

import re
from pathlib import Path

_STATS = (
    Path(__file__).resolve().parents[1]
    / "src/anthropic_throttle_proxy/ui/templates/partials/stats.html"
)


def _focusable_regions() -> list[str]:
    """Every `<section class="bearers-wrap" tabindex="0" ...>` opening tag."""
    return re.findall(r"<section[^>]*class=\"bearers-wrap\"[^>]*>", _STATS.read_text())


def test_every_focusable_region_has_an_id():
    regions = _focusable_regions()
    assert regions, "no scrollable table regions found — did the markup change?"
    missing = [r for r in regions if "id=" not in r]
    assert not missing, (
        "these focusable regions have no id, so htmx cannot restore focus after "
        f"the 2 s swap: {missing}"
    )


def test_region_ids_are_unique():
    markup = _STATS.read_text()
    ids = re.findall(r"<section[^>]*id=\"([^\"]+)\"[^>]*class=\"bearers-wrap\"", markup)
    ids += re.findall(r"<section[^>]*class=\"bearers-wrap\"[^>]*id=\"([^\"]+)\"", markup)
    assert len(ids) == len(set(ids)), f"duplicate region ids break focus restoration: {ids}"
