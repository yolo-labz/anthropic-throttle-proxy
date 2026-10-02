"""Bounded browser acceptance: PYTHONPATH=src:tests python tests/check_viewport.py.

Uses the existing nine-seat fixture, or --url http://127.0.0.1:8765/ui for the
installed dashboard. BROWSER_EXECUTABLE selects an installed Chromium; no operator
profile is attached. Receipts go into the feature spec, never disposable scratch.
"""

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright
from render_preview import main as render_preview


def check(page, url, output):
    receipts = []
    for width, height in ((1366, 768), (1440, 900), (1920, 1080), (2210, 1240), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        assert page.goto(url).status == 200
        page.locator(".board").wait_for()
        dimensions = page.evaluate("""() => ({
            width: innerWidth, height: innerHeight,
            contentWidth: document.documentElement.scrollWidth,
            contentHeight: document.documentElement.scrollHeight,
            blocks: [...document.querySelectorAll(
                'header,.hero,.telemetry,.board,.rail,footer,tr[data-subscription-id]'
            )].map(e => ({
                id: e.id || e.className || e.tagName,
                height: Math.round(e.getBoundingClientRect().height)
            })),
            panels: [...document.querySelectorAll('.bearers-wrap')].map(e => ({
                id: e.id, width: e.clientWidth, contentWidth: e.scrollWidth
            }))
        })""")
        print(json.dumps(dimensions), flush=True)
        page.screenshot(path=str(output / f"viewport-{width}.png"), full_page=True)
        assert dimensions["contentWidth"] <= width, dimensions
        rows = page.locator("tr[data-subscription-id]")
        assert rows.count() == 9
        assert page.locator(".tps-panel").is_visible()
        assert page.locator(".gauges").is_visible()
        if width >= 1280:
            assert dimensions["contentHeight"] <= height, dimensions
            assert all(p["contentWidth"] <= p["width"] + 1 for p in dimensions["panels"])
            for row in rows.all():
                box = row.bounding_box()
                assert box and box["y"] >= 0 and box["y"] + box["height"] <= height, box
            footer = page.locator("footer").bounding_box()
            assert footer and footer["y"] + footer["height"] <= height
        assert page.locator(".sub-ident .acct").first.evaluate(
            "e => parseFloat(getComputedStyle(e).fontSize) >= 14"
        )
        # A native checkbox outside the swapped fragment keeps the user's choice
        # while the fragment (including its detailed readings) remains fresh.
        toggle = page.locator("#show-details")
        assert not toggle.is_checked()
        assert not page.locator(".ident-meta").first.is_visible()
        toggle.focus()
        page.keyboard.press("Space")
        assert page.locator(".ident-meta").first.is_visible()
        assert page.locator(".reset-at").first.is_visible()
        page.evaluate("document.querySelector('#stats').innerHTML += '<!-- poll -->'")
        assert toggle.is_checked()
        assert page.locator(".ident-meta").first.is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        toggle.uncheck()
        receipts.append(dimensions)
    (output / "viewport.json").write_text(json.dumps(receipts, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=Path("specs/289-viewport-fit"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    preview = args.output.resolve() / "preview.html"
    if not args.url:
        render_preview(preview)
        preview.write_text(preview.read_text().replace(str(repo) + "/", "/"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("BROWSER_EXECUTABLE"), headless=True
        )
        page = browser.new_page()
        url = args.url or "http://preview.test/"
        if not args.url:
            # ponytail: Playwright serves the two fixture assets; no HTTP server
            # or operator profile, and all other requests (including CDN) fail.
            assets = {
                url: preview,
                "http://preview.test/src/anthropic_throttle_proxy/ui/static/style.css": (
                    repo / "src/anthropic_throttle_proxy/ui/static/style.css"
                ),
            }
            page.route(
                "**/*",
                lambda route: (
                    route.fulfill(path=str(assets[route.request.url]))
                    if route.request.url in assets
                    else route.abort()
                ),
            )
        check(page, url, args.output)
        browser.close()


if __name__ == "__main__":
    main()
