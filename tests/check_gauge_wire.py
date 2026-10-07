"""Bounded isolated browser receipt: uv run --group delivery python tests/check_gauge_wire.py.

No operator profile, credentials, live mutations, inference or runtime activation.
Live GETs retain only the gauge's public label/value. Branch server uses synthetic
telemetry and collectors that raise if a render tries networking.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from ui_render import workload_snapshot

from anthropic_throttle_proxy import fleet_ui_config, history
from anthropic_throttle_proxy.ui import routes

OUT = Path("docs/gauge-widget-2026-10-07-wire.json")


async def forbidden(*args, **kwargs):
    raise AssertionError("render attempted a network collector")


async def main():
    receipt = {"scope": "live read-only GETs + isolated branch route/browser; not deployed"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        live = []
        for port in (8765, 8773):
            async with session.get(f"http://127.0.0.1:{port}/ui/stats") as response:
                html = await response.text()
                label = re.search(r'<svg class="tps-arc".*?aria-label="([^"]+)"', html, re.S)
                value = re.search(r'<span class="tps-num">([^<]+)</span>', html)
                live.append(
                    {
                        "port": port,
                        "status": response.status,
                        "arc_label": label.group(1) if label else None,
                        "value": value.group(1) if value else None,
                    }
                )
        receipt["live"] = live
    history.reset()
    now = routes.time.time()
    row = workload_snapshot()
    with (
        patch.object(routes._config, "FLEET_HEALTH_URLS", "mimo:http://example.test/health"),
        patch.object(routes._config, "COPILOT_TOKEN", ""),
        patch.object(routes._fleet, "_cache", {"http://example.test/health": (now, row)}),
        patch.object(routes._accounts, "account_view", lambda *args: []),
        patch.object(routes._accounts, "bearer_labels", lambda: {}),
        patch.object(routes._accounts, "refresh_endpoint", forbidden),
        patch.object(routes._fleet, "refresh", forbidden),
        patch.object(routes._copilot, "refresh", forbidden),
        patch.object(routes._proxy, "bearer_state", {}),
        patch.object(fleet_ui_config, "load", lambda: {"subscriptions": [], "defaults": {}}),
    ):
        app = web.Application()
        routes.attach_ui(app)
        app.on_startup.clear()
        app.on_cleanup.clear()
        async with TestServer(app) as server, async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=os.environ["BROWSER_EXECUTABLE"],
                headless=True,
                args=["--disable-gpu"],
            )
            try:
                page = await browser.new_page(viewport={"width": 1366, "height": 768})
                polls = []
                page.on(
                    "response",
                    lambda response: (
                        polls.append(response.url) if "/ui/stats" in response.url else None
                    ),
                )
                response = await page.goto(str(server.make_url("/ui?source=mimo")))
                assert response.status == 200
                await page.wait_for_function("window.htmx !== undefined")
                await page.wait_for_timeout(2400)  # own bounded HTMX polling sample
                assert len(polls) >= 2, polls
                assert await page.locator(".tps-num").inner_text() == "120"
                assert "120 output tokens per second" in await page.locator(
                    ".tps-arc"
                ).get_attribute("aria-label")
                measured = {
                    "value": "120",
                    "polls": len(polls),
                    "all_polls_keep_source": all("source=mimo" in url for url in polls),
                }
                assert measured["all_polls_keep_source"]
                row["ok"] = False
                await page.wait_for_function(
                    "document.querySelector('.tps-panel').textContent"
                    ".includes('throughput unavailable')"
                )
                measured["failure_refresh"] = await page.locator(".tps-panel").inner_text()
                receipt["branch_measured"] = measured
                await page.goto(str(server.make_url("/ui?source=local")))
                assert await page.locator(".tps-value").count() == 0
                unknown = {
                    "arc_label": await page.locator(".tps-arc").get_attribute("aria-label"),
                    "value": await page.locator(".tps-num").inner_text(),
                }
                assert "unknown" in unknown["arc_label"]
                receipt["branch_unknown"] = unknown
                viewports = []
                for width in (1366, 390):
                    await page.set_viewport_size({"width": width, "height": 844})
                    dims = await page.evaluate(
                        "({width: innerWidth, content: document.documentElement.scrollWidth})"
                    )
                    assert dims["content"] <= dims["width"], dims
                    viewports.append(dims)
                receipt["viewports"] = viewports
            finally:
                await browser.close()
    await asyncio.to_thread(OUT.write_text, json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
