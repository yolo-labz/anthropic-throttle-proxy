"""Produce the browser receipt `verify-live.py` demands, in both engines.

Source tests and a green CI are not delivery, and a page that renders in one
engine at one width is not rendering. This drives the LIVE dashboard through
**Firefox and Chromium** at four widths and two zoom levels, runs the six
checks the delivery gate names, and writes the JSON receipt plus one screenshot
per case:

    LD_LIBRARY_PATH="$(specs/227-dashboard-audit/playwright-browser-libs.sh)" \
      uv run --group delivery python specs/227-dashboard-audit/browser-receipt.py
    DASHBOARD_BROWSER_RECEIPT=/tmp/throttle-browser-receipt/receipt.json \
      python3 specs/227-dashboard-audit/verify-live.py

`playwright` lives in the `delivery` dependency group (`uv sync --group delivery`)
rather than `dev`, because a default `uv sync` should not pull a ~90 MB browser
backend for a gate CI deliberately does not run. Chromium needs no library path
on NixOS; Firefox does, which is why `playwright-browser-libs.sh` exists next to
this file. This module deliberately does not shell out to discover that path —
building an `LD_LIBRARY_PATH` from the store means running the browser to see
what it is missing, and a delivery gate that executes a browser in order to
decide how to execute a browser is a gate nobody will trust.

Zoom is emulated the way a browser means it: at 200% the CSS viewport HALVES
and the device scale doubles, so the layout is exercised at the real effective
width rather than being a 1440px layout photographed larger.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ENGINES = ("firefox", "chromium")
WIDTHS = (390, 768, 1440, 2210)
ZOOMS = (100, 200)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/anthropic_throttle_proxy"
# The same file set `verify-live.py` hashes: the receipt must belong to the
# source the gate is judging, or it is evidence about some other candidate.
FILES = (
    "fleet_ui_config.py",
    "lanes.py",
    "ui/routes.py",
    "ui/presentation.py",
    "ui/templates/dashboard.html",
    "ui/templates/partials/stats.html",
    "ui/static/style.css",
)


def source_hash() -> str:
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode())
        digest.update((SOURCE / name).read_bytes())
    return digest.hexdigest()


async def _capture(engine_name: str, width: int, zoom: int, url: str, out_dir: Path) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await getattr(playwright, engine_name).launch()
        page = await browser.new_page(
            viewport={"width": width * 100 // zoom, "height": 900},
            device_scale_factor=zoom / 100,
        )
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(2500)

        # `headings` — a title and section headings that carry real text. A
        # heading with no text, or one that paints at zero size, is a header
        # block that failed to render rather than a heading that is short.
        headings = await page.evaluate(
            """() => {
              const els = [...document.querySelectorAll('h1, h2')];
              return els.length > 0 && els.every(el => {
                const cs = getComputedStyle(el);
                return el.textContent.trim().length > 0
                  && cs.display !== 'none' && cs.visibility !== 'hidden'
                  && parseFloat(cs.fontSize) > 0;
              });
            }"""
        )

        # `scroll` — each panel scrolls WITHIN itself, or fits. A panel that
        # cannot scroll clips its own last column, and one that scrolls when its
        # content fits has a phantom edge. Page-level overflow is its own named
        # check below, deliberately not folded in here: a check that reports two
        # different faults as one is a check nobody can act on.
        scroll = await page.evaluate(
            """() => {
              const panels = [...document.querySelectorAll('.bearers-wrap')];
              if (!panels.length) return false;
              for (const panel of panels) {
                const before = panel.scrollLeft;
                panel.scrollLeft = 9999;
                const moved = panel.scrollLeft - before;
                const overflow = panel.scrollWidth - panel.clientWidth;
                panel.scrollLeft = before;
                if (overflow > 1 && moved <= 0) return false;   // should scroll, does not
                if (overflow <= 1 && moved > 0) return false;   // scrolls when it fits
              }
              return true;
            }"""
        )

        # `refresh` — the HTMX poll is alive: the render-time stamp advances.
        first = await page.evaluate("document.querySelector('#stats .as-of')?.textContent ?? ''")
        await page.wait_for_timeout(4000)
        second = await page.evaluate("document.querySelector('#stats .as-of')?.textContent ?? ''")
        refresh = bool(first) and bool(second) and first != second

        # `focus` — htmx restores focus across the 2 s swap for an element that
        # carries an id (WCAG 2.1 SC 3.2.5). Measured, not assumed.
        await page.focus("#subs-scroll")
        await page.wait_for_timeout(3000)
        focus = await page.evaluate(
            "document.activeElement && document.activeElement.id === 'subs-scroll'"
        )

        # `disconnect` — kill the poll and the CSS staleness cue must fire. This
        # is the page's only disconnect signal and it is the reason the panel
        # needs no watchdog script.
        await page.route("**/ui/stats*", lambda route: asyncio.ensure_future(route.abort()))
        await page.wait_for_timeout(8000)
        disconnect = await page.evaluate(
            """() => {
              const el = document.querySelector('#stats .as-of');
              return !!el && getComputedStyle(el, '::after').content.includes('last known');
            }"""
        )
        await page.unroute("**/ui/stats*")

        no_page_overflow = await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        )

        shot = out_dir / f"case-{engine_name}-{width}-{zoom}.png"
        await page.screenshot(path=str(shot), full_page=True)
        await browser.close()

    return {
        "engine": engine_name,
        "width": width,
        "zoom": zoom,
        "headings": bool(headings),
        "scroll": bool(scroll),
        "refresh": bool(refresh),
        "focus": bool(focus),
        "disconnect": bool(disconnect),
        "no_page_overflow": bool(no_page_overflow),
        "screenshot": shot.name,
        "screenshot_sha256": hashlib.sha256(shot.read_bytes()).hexdigest(),
    }


def _ensure_playwright() -> None:
    """Explain how to get an interpreter that has playwright, or return.

    Playwright is not a project dependency: it is a ~90 MB browser bundle used
    by a delivery gate that CI deliberately does not run, and pulling it into
    `uv sync` would tax every `pytest` for a tool most runs never touch. So the
    import is PROBED, and on failure this names where to get one rather than
    guessing — the tool is meant to be invoked directly, not to re-execute
    itself into a different Python.
    """
    if importlib.util.find_spec("playwright") is not None:
        return

    found = _shebang_interpreter(Path.home() / ".local/bin/playwright")
    also = f"\nThis host already has one at {found}.\n" if found else ""
    raise SystemExit(
        f"playwright is not importable from {sys.executable}.\n"
        "It is declared in the `delivery` dependency group, so:\n\n"
        "    uv sync --group delivery\n"
        '    LD_LIBRARY_PATH="$(specs/227-dashboard-audit/playwright-browser-libs.sh)" \\\n'
        "      uv run --group delivery python specs/227-dashboard-audit/browser-receipt.py\n"
        f"{also}"
        "(--engines chromium needs no Firefox libraries, but still needs playwright.)\n"
    )


def _shebang_interpreter(script: Path) -> str | None:
    """The interpreter a console-script's shebang names, if it is readable."""
    try:
        first = script.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    return first[2:].strip() if first.startswith("#!") else None


def _require_firefox_libraries(engines: tuple[str, ...]) -> None:
    """Fail loudly, before launching, when Firefox cannot possibly start.

    Better a one-line instruction than sixteen identical launch failures
    twenty seconds further in.
    """
    if "firefox" not in engines or os.environ.get("LD_LIBRARY_PATH"):
        return
    sys.stderr.write(
        "Firefox needs its libraries on NixOS:\n"
        '  LD_LIBRARY_PATH="$(specs/227-dashboard-audit/playwright-firefox-libs.sh)" \\\n'
        "    uv run python specs/227-dashboard-audit/browser-receipt.py\n"
        "(or pass --engines chromium to check one engine only)\n"
    )
    raise SystemExit(2)


async def _run(args: argparse.Namespace, engines: tuple[str, ...], widths: tuple[int, ...]) -> int:
    out_dir = Path(args.out)
    os.makedirs(out_dir, exist_ok=True)

    cases = []
    for engine in engines:
        for width in widths:
            for zoom in ZOOMS:
                case = await _capture(engine, width, zoom, args.url, out_dir)
                cases.append(case)
                failed = sorted(k for k, v in case.items() if v is False)
                print(
                    f"{engine:9} {width:>5}px@{zoom:>3}%  "
                    f"{'PASS' if not failed else 'FAIL ' + ','.join(failed)}",
                    flush=True,
                )

    receipt = {
        "source_sha256": source_hash(),
        "observed_epoch": time.time(),
        "url": args.url,
        "cases": cases,
    }
    path = out_dir / "receipt.json"
    path.write_text(json.dumps(receipt, indent=1), encoding="utf-8")
    failed = sum(1 for case in cases for value in case.values() if value is False)
    print(f"\nreceipt: {path}  ({len(cases)} cases, {failed} failed)")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/ui")
    parser.add_argument(
        "--out", default=str(Path(tempfile.gettempdir()) / "throttle-browser-receipt")
    )
    parser.add_argument("--engines", default=",".join(ENGINES))
    parser.add_argument("--widths", default=",".join(str(w) for w in WIDTHS))
    args = parser.parse_args()

    engines = tuple(e.strip() for e in args.engines.split(",") if e.strip())
    widths = tuple(int(w) for w in args.widths.split(",") if w.strip())
    _require_firefox_libraries(engines)
    _ensure_playwright()
    # Playwright validates the host against distributions it models and refuses
    # to launch on NixOS even when the browser demonstrably runs.
    os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")
    return asyncio.run(_run(args, engines, widths))


if __name__ == "__main__":
    sys.exit(main())
