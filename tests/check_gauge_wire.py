"""Bounded isolated browser receipt: uv run --group delivery python tests/check_gauge_wire.py.

No operator profile, credentials, live mutations, inference or runtime activation.
Receipts retain sanitized DOM/screenshots plus public gauge values, not raw
account payloads. Branch telemetry is synthetic; render network I/O raises.
Run within heavy admission with BROWSER_EXECUTABLE set to installed Chromium.
Use --synthetic-only on a CI/off-host allocation without desktop loopback URLs.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from ui_render import workload_snapshot

from anthropic_throttle_proxy import fleet_ui_config, history
from anthropic_throttle_proxy.ui import routes

OUT = Path("docs/evidence/ui-browser-qa-2026-10-07")


def sanitize(text):
    """Keep public UI evidence, never identities, JWTs or bearer hashes."""
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[account]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}\b", "[token]", text)
    text = re.sub(r"(?:R\$|US\$|[$€¥])\s*[\d,.]+", "[billing]", text)
    return re.sub(r"\b[a-f0-9]{8}\b", "[bearer]", text)


async def capture(page, out, name):
    # Sanitize only this owned ephemeral page before screenshot/DOM export.
    await page.evaluate(r"""() => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) walker.currentNode.textContent = walker.currentNode.textContent
        .replace(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/g, '[account]')
        .replace(/(?:R\$|US\$|[$€¥])\s*[\d,.]+/g, '[billing]')
        .replace(/\b[a-f0-9]{8}\b/g, '[bearer]');
    }""")
    await page.screenshot(path=str(out / f"{name}.png"), full_page=False)
    (out / f"{name}.html").write_text(sanitize(await page.content()))
    return await page.evaluate("""() => {
      const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
      const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
      return {
        width: innerWidth, content: document.documentElement.scrollWidth,
        duplicate_ids: ids.filter((id, i) => ids.indexOf(id) !== i),
        main_landmarks: document.querySelectorAll('main').length,
        h1_count: document.querySelectorAll('h1').length,
        unnamed_inputs: [...document.querySelectorAll('input,select,textarea')]
          .filter(visible).filter(e => !e.labels?.length && !e.getAttribute('aria-label')
            && !e.getAttribute('aria-labelledby') && e.type !== 'hidden').map(e => e.id),
        small_targets: [...document.querySelectorAll('a,button,input')].filter(visible)
          .map(e => ({tag: e.tagName, id: e.id, width: e.getBoundingClientRect().width,
            height: e.getBoundingClientRect().height}))
          .filter(e => e.width < 24 || e.height < 24),
        overflow: [...document.querySelectorAll('body *')].filter(visible)
          .filter(e => !e.closest('.bearers-wrap')
            && e.getBoundingClientRect().right > innerWidth + 1)
          .map(e => ({tag: e.tagName, cls: e.getAttribute('class'),
            right: e.getBoundingClientRect().right})).slice(0, 30),
        gauge_clip: [...document.querySelectorAll('.tps-panel *')].filter(visible)
          .filter(e => !e.children.length && e.clientWidth && e.scrollWidth > e.clientWidth + 1)
          .map(e => ({tag: e.tagName, cls: e.getAttribute('class'),
            overflow: e.scrollWidth - e.clientWidth})),
      };
    }""")


async def forbidden(*args, **kwargs):
    raise AssertionError("render attempted network I/O")


async def main(out=OUT, live=True):
    from playwright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    receipt = {
        "scope": "live GETs + synthetic workload/real read-only lane-report snapshot; not deployed"
    }
    findings = []
    receipt["source_sha256"] = {
        str(p.relative_to(Path(routes.__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(routes.__file__).parent.rglob("*")
        if p.suffix in {".html", ".css", ".py"}
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        live_rows = []
        for port in (8765, 8773) if live else ():
            async with session.get(f"http://127.0.0.1:{port}/ui/stats") as response:
                html = await response.text()
                label = re.search(r'<svg class="tps-arc".*?aria-label="([^"]+)"', html, re.S)
                value = re.search(r'<span class="tps-num">([^<]+)</span>', html)
                live_rows.append(
                    {
                        "port": port,
                        "status": response.status,
                        "arc_label": label.group(1) if label else None,
                        "value": value.group(1) if value else None,
                    }
                )
        receipt["live"] = live_rows
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
        patch.object(aiohttp.ClientSession, "_request", forbidden),
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
                receipt["browser"] = browser.version
                page = await browser.new_page(
                    viewport={"width": 1366, "height": 768}, has_touch=True
                )
                page.set_default_timeout(10000)
                console = []
                page.on("pageerror", lambda error: console.append(sanitize(str(error))))
                if live:
                    await page.route("**/ui/stats*", lambda route: route.abort())
                    receipt["live_viewports"] = []
                    for width in (1366, 390):
                        await page.set_viewport_size({"width": width, "height": 844})
                        await page.goto("http://127.0.0.1:8765/ui")
                        receipt["live_viewports"].append(await capture(page, out, f"live-{width}"))
                await page.unroute("**/ui/stats*")
                routes._fleet._cache["http://example.test/health"] = (routes.time.time(), row)
                await page.set_viewport_size({"width": 1366, "height": 768})
                requests = []
                page.on("request", lambda request: requests.append(request.url))
                page.on(
                    "console",
                    lambda message: (
                        console.append(sanitize(message.text)) if message.type == "error" else None
                    ),
                )
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
                selected = page.locator('.workload-picker a[aria-current="page"]')
                assert await selected.inner_text() == "mimo"
                # Native detail preference is outside the swap; region focus is inside it.
                toggle = page.locator("#show-details")
                await toggle.focus()
                await page.keyboard.press("Space")
                assert await toggle.is_checked()
                region = page.locator('.bearers-wrap[tabindex="0"]').first
                region_id = await region.get_attribute("id")
                assert region_id
                await region.focus()
                await page.wait_for_timeout(2400)
                assert await page.evaluate("document.activeElement.id") == region_id
                assert await toggle.is_checked()
                measured["focus_after_poll"] = region_id
                receipt["branch_measured"] = measured
                measured["focus_style"] = await region.evaluate(
                    "e => ({outline: getComputedStyle(e).outline, "
                    "boxShadow: getComputedStyle(e).boxShadow})"
                )
                receipt["synthetic_viewports"] = []
                for width in (1366, 390):
                    await page.set_viewport_size({"width": width, "height": 844})
                    dims = await capture(page, out, f"synthetic-measured-{width}")
                    receipt["synthetic_viewports"].append(dims)
                    if dims["content"] > dims["width"]:
                        findings.append({"kind": "page_overflow", "viewport": dims})
                    if dims["duplicate_ids"] or dims["unnamed_inputs"] or dims["gauge_clip"]:
                        findings.append({"kind": "structural_a11y_or_clip", "viewport": dims})
                    if dims["main_landmarks"] != 1 or dims["h1_count"] != 1:
                        findings.append({"kind": "landmarks", "viewport": dims})
                url = "http://example.test/health"
                routes._fleet._cache[url] = (now - 60, row)
                await page.wait_for_function(
                    "document.querySelector('.tps-panel').textContent"
                    ".includes('throughput unavailable')"
                )
                assert await page.locator(".tps-value").count() == 0
                receipt["synthetic_stale"] = {
                    "cache_age_s": 60,
                    "display": await page.locator(".tps-panel").inner_text(),
                }
                await capture(page, out, "synthetic-stale-390")
                async with page.expect_response(lambda response: "/ui/stats" in response.url):
                    routes._fleet._cache[url] = (routes.time.time(), row)
                    row["ok"] = False
                await page.wait_for_function(
                    "document.querySelector('.tps-panel').textContent"
                    ".includes('throughput unavailable')"
                )
                assert await page.locator(".tps-value").count() == 0
                measured["failure_refresh"] = await page.locator(".tps-panel").inner_text()
                receipt["branch_measured"] = measured
                await capture(page, out, "synthetic-error-390")
                # Exercise a real native link by keyboard, not page.goto selection.
                await page.locator('.workload-picker a[href="/ui?source=local"]').focus()
                await page.keyboard.press("Enter")
                await page.wait_for_url("**/ui?source=local")
                assert await page.locator(".tps-value").count() == 0
                unknown = {
                    "arc_label": await page.locator(".tps-arc").get_attribute("aria-label"),
                    "value": await page.locator(".tps-num").inner_text(),
                }
                assert "unknown" in unknown["arc_label"]
                receipt["branch_unknown"] = unknown
                await capture(page, out, "synthetic-unknown-390")
                await page.locator('.workload-picker a[href="/ui?source=mimo"]').tap()
                await page.wait_for_url("**/ui?source=mimo")
                receipt["narrow_touch"] = "synthetic Chromium touch: native source link"
                unexpected = [
                    u
                    for u in requests
                    if not (
                        u.startswith(str(server.make_url("/")))
                        or u.startswith("https://unpkg.com/htmx.org@1.9.12")
                    )
                ]
                receipt["unexpected_requests"] = unexpected
                receipt["console_errors"] = console
                assert not unexpected, unexpected
                assert not console, console
                receipt["render_network_guard"] = "ClientSession._request and collectors raise"
                viewports = []
                for width in (1366, 390):
                    await page.set_viewport_size({"width": width, "height": 844})
                    dims = await page.evaluate(
                        "({width: innerWidth, content: document.documentElement.scrollWidth})"
                    )
                    viewports.append(dims)
                    if dims["content"] > dims["width"]:
                        findings.append({"kind": "unknown_view_overflow", "viewport": dims})
                receipt["viewports"] = viewports
                receipt["findings"] = findings
                receipt["checks_executed"] = True
                assert not findings, findings
                receipt["completed"] = True
            except Exception as error:
                receipt["failure"] = sanitize(str(error))
                raise
            finally:
                await browser.close()
                (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    await asyncio.to_thread((out / "receipt.json").write_text, json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--synthetic-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.out, not args.synthetic_only))
