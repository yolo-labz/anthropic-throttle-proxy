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
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from ui_render import workload_snapshot

from anthropic_throttle_proxy import fleet_ui_config, history, output_usage
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
        scroll_regions: [...document.querySelectorAll('.bearers-wrap')].map(e => ({
          id: e.id, width: e.clientWidth, content: e.scrollWidth,
          position: getComputedStyle(e).position, overflowX: getComputedStyle(e).overflowX,
          positioned: [...e.querySelectorAll('*')]
            .filter(child => getComputedStyle(child).position === 'absolute')
            .map(child => ({tag: child.tagName, cls: child.getAttribute('class'),
              right: child.getBoundingClientRect().right,
              containing_block: child.offsetParent?.id || child.offsetParent?.tagName,
              contained: e.contains(child.offsetParent), clip: getComputedStyle(child).clip}))
        })),
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


async def quota_accessibility(page):
    """Clipping must preserve quota text in Chromium's actual accessibility tree."""
    client = await page.context.new_cdp_session(page)
    try:
        expected = await page.locator(".bearers-wrap .pct .sr-only").all_text_contents()
        assert expected, "fixture must contain quota annotations"
        await client.send("Accessibility.enable")
        tree = await client.send("Accessibility.getFullAXTree")
        readable = [
            node.get("name", {}).get("value", "").strip()
            for node in tree["nodes"]
            if not node.get("ignored") and node.get("role", {}).get("value") == "StaticText"
        ]
        annotations = []
        for expected_text in expected:
            text = expected_text.strip()
            matches = [name for name in readable if text in name]
            assert matches, {"missing_quota": text}
            readable.remove(matches[0])  # Each DOM annotation needs an exposed text node.
            annotations.append({"text": sanitize(text), "accessible": True})
        return annotations
    finally:
        await client.detach()


async def check_keyboard_scroll(page):
    region = page.locator("#subs-scroll")
    assert await region.evaluate("e => e.scrollWidth > e.clientWidth"), "fixture must scroll"
    assert await region.evaluate("e => getComputedStyle(e).overflowX === 'auto'")
    await region.evaluate("e => { e.scrollLeft = 0; }")
    await region.focus()
    await page.keyboard.press("ArrowRight")
    await page.wait_for_function("document.querySelector('#subs-scroll').scrollLeft > 0")
    result = await region.evaluate("""e => ({
      width: e.clientWidth, content: e.scrollWidth, scrolled: e.scrollLeft,
      focused: document.activeElement === e,
      document_width: document.documentElement.scrollWidth, viewport: innerWidth
    })""")
    assert result["focused"], result
    await region.evaluate("e => { e.scrollLeft = 0; }")
    return result


async def launch(engine):
    return await engine.chromium.launch(
        executable_path=os.environ["BROWSER_EXECUTABLE"], headless=True, args=["--disable-gpu"]
    )


async def deployed(out, expected_build):
    """Read-only real service acceptance; never patch live caches/collectors."""
    from playwright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    receipt = {"scope": "deployed desktop; read-only, no synthetic runtime patches"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get("http://127.0.0.1:8765/__throttle/health") as response:
                assert response.status == 200
                health = await response.json()
            build = Path(health["build"])
            receipt["build"] = str(build)
            assert build.is_relative_to(expected_build), receipt["build"]
            source = Path(routes.__file__).parent
            receipt["ui_sha256"] = {}
            for file in source.rglob("*"):
                if file.suffix not in {".html", ".css", ".py"}:
                    continue
                relative = file.relative_to(source)
                assert (build / "ui" / relative).read_bytes() == file.read_bytes(), relative
                receipt["ui_sha256"][str(relative)] = hashlib.sha256(file.read_bytes()).hexdigest()
            async with session.get("http://127.0.0.1:8765/ui/static/style.css") as response:
                assert await response.read() == (source / "static/style.css").read_bytes()
        async with async_playwright() as engine:
            browser = await launch(engine)
            try:
                receipt["browser"] = browser.version
                page = await browser.new_page(viewport={"width": 1366, "height": 844})
                page.set_default_timeout(10000)
                polls, errors, unexpected = [], [], []
                page.on("pageerror", lambda error: errors.append(sanitize(str(error))))
                page.on(
                    "console",
                    lambda message: (
                        errors.append(sanitize(message.text)) if message.type == "error" else None
                    ),
                )
                page.on("response", lambda r: polls.append(r.url) if "/ui/stats" in r.url else None)
                page.on(
                    "request",
                    lambda r: (
                        unexpected.append(r.url)
                        if not (
                            r.url.startswith("http://127.0.0.1:8765/")
                            or r.url.startswith("https://unpkg.com/htmx.org@1.9.12")
                        )
                        else None
                    ),
                )
                receipt["viewports"] = []
                for width in (1366, 390):
                    await page.set_viewport_size({"width": width, "height": 844})
                    await page.goto("http://127.0.0.1:8765/ui?source=local")
                    await page.wait_for_function("window.htmx !== undefined")
                    await page.locator("#show-details").check()
                    region = page.locator('.bearers-wrap[tabindex="0"]').first
                    region_id = await region.get_attribute("id")
                    assert region_id
                    await region.focus()
                    before = len(polls)
                    await page.wait_for_timeout(2400)
                    assert len(polls) > before and all("source=local" in p for p in polls)
                    assert await page.evaluate("document.activeElement.id") == region_id
                    assert await page.locator("#show-details").is_checked()
                    quota_row = page.locator('[data-subscription-id="mimo:desktop-subscription"]')
                    assert await quota_row.count() == 1
                    report_path = (
                        Path.home() / ".local/state/anthropic-throttle-proxy/mimo-desktop.json"
                    )
                    report = json.loads(report_path.read_text())
                    rows = [r for r in report["lanes"] if r["id"] == "mimo:desktop-subscription"]
                    assert len(rows) == 1 and report["intervalSeconds"] == 300
                    meter = rows[0]["meters"][0]
                    assert meter["limitId"] == "weekly" and meter["unit"] == "percent"
                    assert abs(meter["usedPercent"] + meter["remainingPercent"] - 100) < 1e-6
                    age = (
                        datetime.now(UTC) - datetime.fromisoformat(report["generatedAt"])
                    ).total_seconds()
                    assert 0 <= age < 600, age
                    shown = await quota_row.locator(".pct").inner_text()
                    assert abs(float(shown.split("%", 1)[0]) - meter["usedPercent"]) <= 0.51
                    receipt["desktop_quota"] = {
                        "generatedAt": report["generatedAt"],
                        "age_s": age,
                        "unit": "percent",
                        "window": "weekly",
                        "used": meter["usedPercent"],
                        "remaining": meter["remainingPercent"],
                        "display": sanitize(await quota_row.inner_text()),
                    }
                    receipt.setdefault("quota_accessibility", {})[
                        str(width)
                    ] = await quota_accessibility(page)
                    dims = await capture(page, out, f"deployed-{width}")
                    receipt["viewports"].append(dims)
                    assert dims["width"] == dims["content"], dims
                    assert not dims["duplicate_ids"] and not dims["unnamed_inputs"], dims
                receipt["polls"] = len(polls)
                receipt["console_errors"], receipt["unexpected_requests"] = errors, unexpected
                assert not errors and not unexpected
                receipt["completed"] = True
            finally:
                await browser.close()
    except Exception as error:
        receipt["failure"] = sanitize(str(error))
        raise
    finally:
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


async def main(out=OUT, live=True):
    from playwright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    receipt = {
        "scope": ("live GETs + " if live else "")
        + "synthetic client accounting/real read-only lane-report snapshot; not deployed"
    }
    findings = []
    receipt["source_sha256"] = {
        str(p.relative_to(Path(routes.__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(routes.__file__).parent.rglob("*")
        if p.suffix in {".html", ".css", ".py"}
    }
    reader_bytes = await asyncio.to_thread(Path(output_usage.__file__).read_bytes)
    receipt["source_sha256"]["../output_usage.py"] = hashlib.sha256(reader_bytes).hexdigest()
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

    def client_fixture():
        # Synthetic single-domain 1800 output /60s, independent of the 120/s proxy copy.
        return output_usage.Snapshot(
            routes._signals.tps_gauge([[300, 0]] * 6),
            routes.time.time(),
            output_tokens=1800,
            providers=("codex-a", "zai"),
        )

    with (
        patch.object(output_usage, "_cache", client_fixture()),
        patch.object(output_usage, "refresh", forbidden),
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
            browser = await launch(playwright)
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
                assert await page.locator(".tps-num").inner_text() == "30"
                assert "30 output tokens per second" in await page.locator(
                    ".tps-arc"
                ).get_attribute("aria-label")
                measured = {
                    "value": "30",
                    "accounting_domain": "single synthetic client journal; proxy copy excluded",
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
                receipt["state_viewports"] = []
                receipt["quota_accessibility"] = {}
                for width in (1366, 390):
                    await page.set_viewport_size({"width": width, "height": 844})
                    receipt["quota_accessibility"][str(width)] = await quota_accessibility(page)
                    dims = await capture(page, out, f"synthetic-measured-{width}")
                    receipt["synthetic_viewports"].append(dims)
                    if dims["content"] > dims["width"]:
                        findings.append({"kind": "page_overflow", "viewport": dims})
                    if dims["duplicate_ids"] or dims["unnamed_inputs"] or dims["gauge_clip"]:
                        findings.append({"kind": "structural_a11y_or_clip", "viewport": dims})
                    if dims["main_landmarks"] != 1 or dims["h1_count"] != 1:
                        findings.append({"kind": "landmarks", "viewport": dims})
                    if width == 390:
                        receipt["keyboard_scroll"] = await check_keyboard_scroll(page)
                url = "http://example.test/health"
                routes._fleet._cache[url] = (now - 60, row)
                await page.wait_for_function(
                    "document.querySelector('.verdict').textContent.includes('WORKLOAD UNKNOWN')"
                )
                assert await page.locator(".tps-num").inner_text() == "30"
                receipt["synthetic_stale"] = {
                    "cache_age_s": 60,
                    "display": await page.locator(".tps-panel").inner_text(),
                }
                receipt["state_viewports"].append(await capture(page, out, "synthetic-stale-390"))
                async with page.expect_response(lambda response: "/ui/stats" in response.url):
                    routes._fleet._cache[url] = (routes.time.time(), row)
                    row["ok"] = False
                await page.wait_for_function(
                    "document.querySelector('.verdict').textContent.includes('WORKLOAD UNKNOWN')"
                )
                assert await page.locator(".tps-num").inner_text() == "30"
                measured["failure_refresh"] = await page.locator(".tps-panel").inner_text()
                receipt["branch_measured"] = measured
                receipt["state_viewports"].append(await capture(page, out, "synthetic-error-390"))
                output_usage._cache = replace(client_fixture(), sampled_at=now - 60)
                await page.wait_for_function(
                    "document.querySelector('.tps-panel').textContent.includes('cache stale')"
                )
                assert await page.locator(".tps-value").count() == 0
                receipt["state_viewports"].append(
                    await capture(page, out, "synthetic-client-stale-390")
                )
                output_usage._cache = None
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
                receipt["state_viewports"].append(await capture(page, out, "synthetic-unknown-390"))
                output_usage._cache = client_fixture()
                await page.locator('.workload-picker a[href="/ui?source=mimo"]').tap()
                await page.wait_for_url("**/ui?source=mimo")
                assert await page.locator(".tps-num").inner_text() == "30"
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
                    dims = await capture(page, out, f"synthetic-returned-sibling-{width}")
                    viewports.append(dims)
                    if dims["content"] > dims["width"]:
                        findings.append({"kind": "unknown_view_overflow", "viewport": dims})
                receipt["viewports"] = viewports
                for dims in receipt["state_viewports"]:
                    if dims["content"] > dims["width"]:
                        findings.append({"kind": "state_page_overflow", "viewport": dims})
                for dims in (
                    *receipt["synthetic_viewports"],
                    *receipt["state_viewports"],
                    *viewports,
                ):
                    escaped = [
                        child
                        for region in dims["scroll_regions"]
                        for child in region["positioned"]
                        if child["cls"] == "sr-only" and not child["contained"]
                    ]
                    if escaped:
                        findings.append({"kind": "escaped_annotation", "children": escaped})
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
    parser.add_argument("--deployed-build", type=Path, help="exact pV-activated package root")
    args = parser.parse_args()
    if args.deployed_build:
        assert not args.synthetic_only
        asyncio.run(deployed(args.out, args.deployed_build))
    else:
        asyncio.run(main(args.out, not args.synthetic_only))
