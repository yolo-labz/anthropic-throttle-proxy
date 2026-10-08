"""Independent combined completion gauge acceptance. Run within heavy admission.

Synthetic journal fixtures use the real reader in an isolated app, never inference.
--deployed-build is strictly read-only and requires matching source/UI bytes.
"""

import argparse
import asyncio
import hashlib
import json
import math
import re
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestServer
from check_gauge_wire import (
    capture,
    check_keyboard_scroll,
    deployed,
    launch,
    quota_accessibility,
    render_app,
)
from live_journal_oracle import compatible_rates, events, native_read
from ui_render import workload_snapshot

from anthropic_throttle_proxy import history, output_usage
from anthropic_throttle_proxy.ui import routes


def fixture(now, state="measured"):
    def event(age, output, provider):
        return {
            "ts": datetime.fromtimestamp(now - age, UTC).isoformat(),
            "output": output,
            "provider": provider,
            "input": 99999,
            "cacheRead": 88888,
        }

    with tempfile.TemporaryDirectory(prefix="combined-browser-qa-") as directory:
        path = Path(directory) / "usage.jsonl"
        rows = [event(120, 1, "codex-a"), event(30, 600, "codex-b"), event(1, 1200, "zai")]
        if state == "unknown":
            rows = [event(1, 600, "zai")]
        elif state == "unsupported":
            rows[-1]["output"] = None
        elif state == "idle":
            rows = [event(120, 1, "codex-a"), event(90, 0, "zai")]
        if state != "error":
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        sample = output_usage.read_usage(path, now=now)
    return replace(sample, sampled_at=now - 16) if state == "stale" else sample


async def source(out):
    from playwright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    now = routes.time.time()
    snapshot = workload_snapshot()
    history.reset()
    history.observe_tokens(9000, 99999)
    history.record(0, 0, 8, now=now - 1)
    assert history.series()[-1].tok_out == 9000
    receipt = {"scope": "synthetic journals / actual reader+UI; not deployed or live inference"}
    receipt["ignored_local_proxy_bucket_outputs"] = 9000
    receipt["source_sha256"] = {
        str(p.relative_to(Path(routes.__file__).parent.parent)): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in [Path(output_usage.__file__), *Path(routes.__file__).parent.rglob("*")]
        if p.suffix in {".py", ".html", ".css"}
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("render attempted journal/network collection")

    states = {
        state: fixture(now, state)
        for state in ("measured", "idle", "stale", "error", "unknown", "unsupported")
    }
    try:
        with (
            patch.object(routes.time, "time", lambda: now),
            patch.object(output_usage, "_cache", states["measured"]),
            patch.object(output_usage, "read_usage", forbidden),
            render_app(now, snapshot, forbidden) as app,
        ):
            async with TestServer(app) as server, async_playwright() as playwright:
                browser = await launch(playwright)
                try:
                    page = await browser.new_page(
                        viewport={"width": 1366, "height": 844}, has_touch=True
                    )
                    page.set_default_timeout(10000)
                    errors, requests, polls = [], [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: requests.append(request.url))
                    page.on(
                        "response",
                        lambda response: (
                            polls.append(response.url) if "/ui/stats" in response.url else None
                        ),
                    )
                    receipt["browser"] = browser.version
                    receipt["viewports"], receipt["quota_accessibility"] = [], {}
                    for width in (1366, 390):
                        await page.set_viewport_size({"width": width, "height": 844})
                        await page.goto(str(server.make_url("/ui?source=mimo")))
                        await page.wait_for_function("window.htmx !== undefined")
                        await page.locator("#show-details").check()
                        region = page.locator('.bearers-wrap[tabindex="0"]').first
                        await region.focus()
                        region_id = await region.get_attribute("id")
                        before = len(polls)
                        await page.wait_for_timeout(2400)
                        assert len(polls) > before and all("source=mimo" in p for p in polls)
                        assert await page.evaluate("document.activeElement.id") == region_id
                        assert await page.locator("#show-details").is_checked()
                        assert await page.locator(".tps-num").inner_text() == "30"
                        side = await page.locator(".tps-side").text_content()
                        receipt["headline_text"] = side
                        assert "Combined output throughput" in side and "60s" in side
                        assert "accounted at completion" in side and "partial" in side.lower()
                        assert "non-Pi / other hosts unmeasured" in side
                        receipt["quota_accessibility"][str(width)] = await quota_accessibility(page)
                        await page.locator(".tps-panel").scroll_into_view_if_needed()
                        dims = await capture(page, out, f"combined-measured-{width}")
                        receipt["viewports"].append(dims)
                        assert dims["width"] == dims["content"] and not dims["gauge_clip"], dims
                        assert not dims["duplicate_ids"] and not dims["unnamed_inputs"], dims
                        assert dims["main_landmarks"] == dims["h1_count"] == 1, dims
                    receipt["keyboard_scroll"] = await check_keyboard_scroll(page)
                    await page.locator('.workload-picker a[href="/ui?source=local"]').focus()
                    await page.keyboard.press("Enter")
                    await page.wait_for_url("**/ui?source=local")
                    assert await page.locator(".tps-num").inner_text() == "30"
                    await page.locator('.workload-picker a[href="/ui?source=mimo"]').tap()
                    await page.wait_for_url("**/ui?source=mimo")
                    assert await page.locator(".tps-num").inner_text() == "30"
                    receipt["source_selection"] = (
                        "keyboard local / touch mimo / HTMX keep combined30; sibling120 excluded"
                    )
                    receipt["states"] = []
                    for state in ("idle", "stale", "error", "unknown", "unsupported"):
                        output_usage._cache = states[state]
                        reason = (
                            "client accounting cache stale"
                            if state == "stale"
                            else states[state].reason
                        )
                        await page.wait_for_function(
                            "({value, reason}) => "
                            "document.querySelector('.tps-num').textContent === value "
                            "&& (!reason || "
                            "document.querySelector('.tps-side').textContent.includes(reason))",
                            arg={"value": "0" if state == "idle" else "—", "reason": reason},
                        )
                        assert await page.locator(".tps-value").count() == (
                            1 if state == "idle" else 0
                        )
                        if state != "idle":
                            assert "unknown" in await page.locator(".tps-arc").get_attribute(
                                "aria-label"
                            )
                        await page.locator(".tps-panel").scroll_into_view_if_needed()
                        dims = await capture(page, out, f"combined-{state}-390")
                        assert dims["width"] == dims["content"] and not dims["gauge_clip"], dims
                        receipt["states"].append(
                            {
                                "state": state,
                                "value": await page.locator(".tps-num").inner_text(),
                                "geometry": dims,
                            }
                        )
                    receipt["polls"], receipt["console_errors"] = len(polls), errors
                    unexpected = [
                        u
                        for u in requests
                        if not (
                            u.startswith(str(server.make_url("/")))
                            or u.startswith("https://unpkg.com/htmx.org@1.9.12")
                        )
                    ]
                    receipt["unexpected_requests"] = unexpected
                    assert not errors and not unexpected
                    receipt["render_guards"] = (
                        "journal reader, collectors and ClientSession request raise"
                    )
                    after = {
                        str(p.relative_to(Path(routes.__file__).parent.parent)): hashlib.sha256(
                            p.read_bytes()
                        ).hexdigest()
                        for p in [
                            Path(output_usage.__file__),
                            *Path(routes.__file__).parent.rglob("*"),
                        ]
                        if p.suffix in {".py", ".html", ".css"}
                    }
                    assert after == receipt["source_sha256"], "source changed during acceptance"
                    receipt["completed"] = True
                finally:
                    await browser.close()
    except Exception as error:
        receipt["failure"] = str(error)
        raise
    finally:
        history.reset()
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


async def live(out, build):
    receipt = {
        "scope": "exact deployed reader/UI + native real-journal arithmetic; no runtime patches",
        "ui_cached_endpoint": "not exposed; rate checked against cache-valid window bounds",
        "completed": False,
    }
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    try:
        # Retain previous exact UI/CSS, quota, geometry, AX, focus/network oracles.
        await deployed(out, build, combined=True)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get("http://127.0.0.1:8765/__throttle/health") as response:
                root = Path((await response.json())["build"])
            expected_bytes = await asyncio.to_thread(Path(output_usage.__file__).read_bytes)
            deployed_bytes = await asyncio.to_thread((root / "output_usage.py").read_bytes)
            assert (
                hashlib.sha256(deployed_bytes).digest() == hashlib.sha256(expected_bytes).digest()
            )
            receipt["reader_sha256"] = hashlib.sha256(expected_bytes).hexdigest()
            arithmetic, journal = await native_read(root)
            receipt["native_fixed_window"] = arithmetic
            receipt["workloads"] = []
            started, monotonic_start = time.time(), time.monotonic()
            for selected in ("local", "mimo", "local"):
                async with session.get(
                    f"http://127.0.0.1:8765/ui/stats?source={selected}"
                ) as response:
                    html = await response.text()
                    assert response.status == 200
                    assert "Combined output throughput" in html
                    assert "non-Pi / other hosts unmeasured" in html and "tokens/s" in html
                    match = re.search(r'<span class="tps-num">([^<]+)</span>', html)
                    assert match
                    value = match.group(1)
                    if value != "—":
                        assert math.isfinite(float(value)) and float(value) >= 0
                        assert "last 60s" in html and "accounted at completion" in html
                    else:
                        assert "Output throughput unknown" in html and "not a measured zero" in html
                    arc = re.search(r'<svg class="tps-arc".*?aria-label="([^"]+)"', html, re.S)
                    assert arc
                    receipt["workloads"].append(
                        {
                            "selected": selected,
                            "displayed": value,
                            "window_s": 60,
                            "arc_label": arc.group(1),
                            "measurement": "unknown" if value == "—" else "completion-accounted",
                        }
                    )
            ended = time.time()
            assert time.monotonic() - monotonic_start < 2, "triplet crossed collector period"
            brackets = [(r["displayed"], r["arc_label"]) for r in receipt["workloads"]]
            assert brackets[1] in (brackets[0], brackets[2]), "source selection narrowed gauge"
            rows, _, _ = await asyncio.to_thread(events, journal)
            possible = compatible_rates(rows, started - 15, ended)
            for row in receipt["workloads"]:
                assert row["displayed"] != "—", "live combined counter unverified/unknown"
                assert row["displayed"] in possible, "UI rate has no valid real common60s window"
            receipt["ui_window_bounds"] = {
                "start": started - 15,
                "end": ended,
                "candidate_rounded_rates": sorted(possible),
                "scope": "bounds, not an exposed in-process sample timestamp",
            }
            async with session.get("http://127.0.0.1:8765/__throttle/health") as response:
                assert (await response.json())["build"] == str(root), "build changed during packet"
        receipt["completed"] = True
    except Exception as error:
        receipt["failure"] = str(error)
        raise
    finally:
        await asyncio.to_thread(
            (out / "combined-live.json").write_text, json.dumps(receipt, indent=2) + "\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--deployed-build", type=Path)
    args = parser.parse_args()
    asyncio.run(live(args.out, args.deployed_build) if args.deployed_build else source(args.out))
