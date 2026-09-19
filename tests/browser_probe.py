"""Measure a rendered page in a real browser. Not a test, a dev tool.

`tests/render_preview.py` produces the HTML; this reads what a browser actually
laid out. The distinction is the 18/09/2026 lesson: the preview rendered fine
and the stylesheet read fine, and the live page still had a meter cell
overflowing by 50px into its neighbour, because the page-level overflow probe
uses `overflow-x: hidden` as "this scrolls on purpose" — which is true of every
panel — and so never looked inside one.

Requires playwright and a browser. Run against a file or a live URL:

    uv run python tests/browser_probe.py file:///tmp/ui.html 2560
    uv run python tests/browser_probe.py http://127.0.0.1:8765/ui 1440 2560

Reports, per width: page-level horizontal overflow, clipped text anywhere
outside a panel, and clipped text INSIDE a panel (the class this tool exists
for), plus the computed type scale the page actually rendered — so a redesign
can be measured rather than argued about.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sys

_PROBE = r"""
(() => {
  const clipped = (root, skipPanels) => {
    const out = [];
    for (const el of document.querySelectorAll(root)) {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || !el.clientWidth) continue;
      if (skipPanels && el.closest('.bearers-wrap')) continue;
      const over = el.scrollWidth - el.clientWidth;
      if (over > 1) out.push({
        cls: (el.className || '').toString().slice(0, 48),
        text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 72),
        over,
      });
    }
    return out;
  };
  const sizes = {};
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || !el.clientWidth) continue;
    if (el.children.length === 0 && (el.textContent || '').trim()) {
      sizes[cs.fontSize] = (sizes[cs.fontSize] || 0) + 1;
    }
  }
  return {
    doc_width: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
    page_height: document.documentElement.scrollHeight,
    font_sizes: sizes,
    clipped_outside_panels: clipped('body *', true),
    clipped_inside_panels: clipped('.bearers-wrap *', false),
  };
})()
"""


async def _probe(playwright, url: str, widths: list[int]) -> dict:
    chrome = os.environ.get("PROBE_CHROME", "google-chrome")
    browser = await playwright.chromium.launch(executable_path=chrome)
    report = {}
    for width in widths:
        page = await browser.new_page(viewport={"width": width, "height": 1080})
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        report[str(width)] = await page.evaluate(_PROBE)
        await page.close()
    await browser.close()
    return report


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return int(bool(sys.stderr.write(__doc__ or ""))) or 1
    from playwright.async_api import async_playwright

    url = argv[1]
    widths = [int(a) for a in argv[2:]] or [1440, 2560]
    report = asyncio.run(_run(async_playwright, url, widths))
    print(json.dumps(report, indent=1))
    failures = [
        (width, kind, len(rows))
        for width, data in report.items()
        for kind, rows in (
            ("outside panels", data["clipped_outside_panels"]),
            ("inside panels", data["clipped_inside_panels"]),
        )
        if rows
    ]
    for width, kind, count in failures:
        print(f"CLIPPED {count} element(s) {kind} at {width}px", file=sys.stderr)
    print("font sizes rendered:", dict(collections.Counter(report[str(widths[0])]["font_sizes"])))
    return 1 if failures else 0


async def _run(engine, url: str, widths: list[int]) -> dict:
    async with engine() as playwright:
        return await _probe(playwright, url, widths)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
