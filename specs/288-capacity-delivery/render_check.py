"""Run with uv run --group delivery; input is render_preview.py's local fixture."""

import json
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright

preview = Path(sys.argv[1]).resolve()
repo = Path(__file__).resolve().parents[2]
# Existing preview paths target file://; serve only this repository on loopback.
preview.write_text(preview.read_text().replace(str(repo) + "/", "/"))
with ThreadingHTTPServer(
    ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=repo)
) as server:
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE")
            )
            page = browser.new_page()
            # Fixture only: no credentials, operator tabs, CDN or provider calls.
            page.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(origin + "/")
                    else route.abort()
                ),
            )
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                page.goto(f"{origin}/{preview.relative_to(repo)}")
                assert page.locator('.summary[aria-label^="Capacity"]').count() == 1
                assert "MiMo Team owner" in page.inner_text("body")
                assert "MiMo Team — unassigned" in page.inner_text("body")
                dimensions = page.evaluate(
                    "({viewport:innerWidth, content:document.documentElement.scrollWidth})"
                )
                assert dimensions["content"] <= width, dimensions
                page.screenshot(path=str(preview.parent / f"capacity-{width}.png"), full_page=True)
                print(json.dumps(dimensions))
            browser.close()
    finally:
        server.shutdown()
