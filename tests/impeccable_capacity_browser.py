"""Server-browser acceptance for the narrow capacity reflow (synthetic, bounded, no network).

Two processes, one owned file:

* ``--serve`` runs under the repo's own venv (``.venv``). It renders the production
  dashboard through ``render_app`` (the isolation guard shared with the combined
  checks, from ``check_gauge_wire``) on 127.0.0.1 for a bounded time, then exits.
* the default mode runs under ``browser-patchright`` with the supported
  ``interactive.attach`` remote browser. That browser cannot reach this desktop's
  loopback, so the page's own ``http://preview.test`` requests are routed by
  Playwright to the serve process. The pinned SRI HTMX 1.9.12 script loads normally;
  every routed request is recorded, so polls are observed rather than assumed.

Acceptance is real HTMX, not static DOM replacement: after at least two actual
``/ui/stats`` polls that swap ``#stats``, the details checkbox, focus on the capacity
region, the ``mimo`` source selection and the scroll position must survive.
``--negative`` injects a state-losing ``htmx:afterSettle`` handler and the run must
fail. Exit 0 means the mode behaved as designed (positive passes / negative fails).
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ORIGIN = "http://preview.test"
WIDTHS = (390, 768, 1280, 1919)
STATE_WIDTHS = (390, 1280)


def serve(port_file: Path, seconds: int) -> None:
    """Bounded synthetic app: production routes, patched collectors, no state writes."""
    sys.path.insert(0, str(HERE))
    from aiohttp import web
    from check_gauge_wire import render_app
    from ui_render import workload_snapshot

    from anthropic_throttle_proxy import output_usage
    from anthropic_throttle_proxy.ui import routes

    now = time.time()

    def forbidden(*_a, **_k):
        raise AssertionError("render attempted journal/network collection")

    async def run() -> None:
        with (
            patch.object(routes.time, "time", lambda: now),
            patch.object(output_usage, "read_usage", forbidden),
            render_app(now, workload_snapshot(), forbidden) as app,
        ):
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            await asyncio.to_thread(
                port_file.write_text, str(site._server.sockets[0].getsockname()[1])
            )
            await asyncio.sleep(seconds)
            await runner.cleanup()

    asyncio.run(run())


GEOMETRY_JS = """() => {
  const hv = document.querySelector('.hero-verdict');
  const r = hv ? hv.getBoundingClientRect() : {width: 0, height: 0, x: 0, right: 0};
  return {width: innerWidth, dpr: devicePixelRatio,
          page_scroll: document.documentElement.scrollWidth,
          page_client: document.documentElement.clientWidth,
          hero: {w: r.width, h: r.height, x: r.x, right: r.right,
                 scroll: hv ? hv.scrollWidth : 0, client: hv ? hv.clientWidth : 0,
                 font: hv ? parseFloat(getComputedStyle(hv).fontSize) : 0}};
}"""


def geometry_ok(g: dict) -> bool:
    h = g["hero"]
    return (
        h["w"] > 0
        and h["scroll"] <= h["client"] + 1
        and h["font"] >= 10
        and h["x"] >= 0
        and h["right"] <= g["width"] + 1
        and g["page_scroll"] <= g["page_client"] + 1
    )


def htmx_state(page, polls: list, negative: bool) -> dict:
    page.evaluate(
        "() => { window.__swaps = 0; document.body.addEventListener('htmx:afterSettle', e => {"
        " if (e.detail && e.detail.elt && e.detail.elt.id === 'stats') window.__swaps += 1; }); }"
    )
    if negative:
        page.evaluate(
            "() => document.body.addEventListener('htmx:afterSettle', () => {"
            " const c = document.querySelector('#show-details'); if (c) c.checked = false; })"
        )
    page.locator("#show-details").check()
    region = page.locator('.bearers-wrap[tabindex="0"]').first
    region_id = region.get_attribute("id")
    region.focus()
    page.evaluate("() => window.scrollTo(0, 240)")
    state = {
        "scroll_before": page.evaluate("window.scrollY"),
        "focus_id_before": region_id,
    }
    polls_before = len(polls)
    swaps_before = page.evaluate("window.__swaps")
    deadline = time.time() + 14
    while time.time() < deadline and (
        len(polls) < polls_before + 2 or page.evaluate("window.__swaps") < swaps_before + 2
    ):
        page.wait_for_timeout(250)
    state.update(
        {
            "polls_after_focus": len(polls) - polls_before,
            "swaps_after_focus": page.evaluate("window.__swaps") - swaps_before,
            "details_checked": page.locator("#show-details").is_checked(),
            "focus_id_after": page.evaluate("document.activeElement && document.activeElement.id"),
            "focus_outline": page.evaluate(
                "(id) => { const e = document.getElementById(id);"
                " return e ? getComputedStyle(e).outlineStyle : null; }",
                region_id,
            ),
            "source_selected": page.locator('a[aria-current="page"]').first.inner_text(),
            "scroll_after": page.evaluate("window.scrollY"),
        }
    )
    state["pass"] = (
        state["polls_after_focus"] >= 2
        and state["swaps_after_focus"] >= 2
        and state["details_checked"]
        and state["focus_id_after"] == region_id
        and state["source_selected"].strip() == "mimo"
        and abs(state["scroll_after"] - state["scroll_before"]) <= 4
    )
    return state


def zoom_probe(page) -> dict:
    """Empirical native-zoom check: Ctrl+Equal, then restore with Ctrl+0 on this owned page."""
    before = page.evaluate("({w: innerWidth, dpr: devicePixelRatio})")
    page.keyboard.press("Control+Equal")
    page.wait_for_timeout(700)
    after = page.evaluate("({w: innerWidth, dpr: devicePixelRatio})")
    page.keyboard.press("Control+Digit0")
    page.wait_for_timeout(500)
    restored = page.evaluate("({w: innerWidth, dpr: devicePixelRatio})")
    effective = after["dpr"] != before["dpr"] or after["w"] != before["w"]
    return {
        "before": before,
        "after_ctrl_equal": after,
        "restored": restored,
        "native_zoom_effective": effective,
        "pass": effective,
    }


def zoom_only(out: Path, web_repo: Path) -> dict:
    """Native-zoom mechanism check on an inline page: no navigation, no synthetic origin."""
    sys.path.insert(0, str(web_repo))
    from lib import interactive

    out.mkdir(parents=True, exist_ok=True)
    result: dict = {"mechanism": "keyboard Control+Equal / Control+Digit0 via remote CDP input"}
    try:
        with interactive.attach("brave-search", start_if_down=False, timeout_ms=20000) as (
            _pw,
            _browser,
            _ctx,
            page,
        ):
            page.set_content("<!doctype html><title>zoom probe</title><p>zoom</p>")
            page.set_viewport_size({"width": 1280, "height": 800})
            result["zoom"] = zoom_probe(page)
    except Exception as error:  # receipt, not crash
        result["error"] = f"{type(error).__name__}: {str(error)[:300]}"
    (out / "zoom-probe.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def browse(out: Path, port: int, negative: bool, web_repo: Path) -> dict:
    sys.path.insert(0, str(web_repo))
    from lib import interactive

    out.mkdir(parents=True, exist_ok=True)
    receipt: dict = {
        "negative": negative,
        "started": datetime.now(UTC).isoformat(),
        "port": port,
        "geometry": [],
        "htmx": {},
        "failures": [],
    }
    polls: list[str] = []

    def route(r):
        url = r.request.url
        if not url.startswith(ORIGIN):
            r.continue_()  # the pinned SRI HTMX script (and nothing else) loads normally
            return
        path = url[len(ORIGIN) :] or "/"
        if path.startswith("/ui/stats"):
            polls.append(path)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            ctype = resp.getheader("Content-Type", "text/html")
            r.fulfill(status=resp.status, headers={"Content-Type": ctype}, body=body)
        finally:
            conn.close()

    try:
        with interactive.attach("brave-search", start_if_down=False, timeout_ms=20000) as (
            _pw,
            _browser,
            _ctx,
            page,
        ):
            page.set_default_timeout(15000)
            page.route("**/*", route)
            page.emulate_media(reduced_motion="reduce")
            for w in WIDTHS:
                page.set_viewport_size({"width": w, "height": 992})
                page.goto(f"{ORIGIN}/ui?source=mimo", wait_until="load")
                page.wait_for_function("window.htmx !== undefined", timeout=20000)
                g = page.evaluate(GEOMETRY_JS)
                g["pass"] = geometry_ok(g)
                receipt["geometry"].append(g)
                if not g["pass"]:
                    receipt["failures"].append(f"geometry {w}")
                if w in STATE_WIDTHS:
                    s = htmx_state(page, polls, negative)
                    receipt["htmx"][str(w)] = s
                    if not s["pass"]:
                        receipt["failures"].append(f"htmx state lost at {w}")
                page.screenshot(path=str(out / f"capacity-{w}.png"), full_page=True)
            receipt["zoom"] = zoom_probe(page)
            if not receipt["zoom"]["pass"]:
                receipt["failures"].append("native zoom not effective (innerWidth/dpr unchanged)")
            receipt["poll_count"] = len(polls)
            receipt["routed_stats_paths"] = sorted(set(polls))[:5]
    except Exception as error:  # browser/transport errors are receipts, not crashes
        receipt["failures"].append(f"browser: {type(error).__name__}: {str(error)[:300]}")
    receipt["ok"] = not receipt["failures"]
    name = "receipt-negative.json" if negative else "receipt.json"
    (out / name).write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    return receipt


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--serve", action="store_true", help="run the bounded synthetic app (venv)")
    ap.add_argument("--port-file", type=Path)
    ap.add_argument("--seconds", type=int, default=240)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--port", type=int)
    ap.add_argument("--web-repo", type=Path)
    ap.add_argument("--negative", action="store_true")
    ap.add_argument("--zoom-only", action="store_true", help="native-zoom mechanism probe only")
    a = ap.parse_args()
    if a.serve:
        serve(a.port_file, a.seconds)
        return
    if a.zoom_only:
        result = zoom_only(a.out, a.web_repo)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result.get("zoom", {}).get("pass") else 1)
    receipt = browse(a.out, a.port, a.negative, a.web_repo)
    print(json.dumps({"ok": receipt["ok"], "failures": receipt["failures"]}, ensure_ascii=False))
    expected = not a.negative  # positive must pass; the negative control must fail
    raise SystemExit(0 if receipt["ok"] == expected else 1)


if __name__ == "__main__":
    main()
