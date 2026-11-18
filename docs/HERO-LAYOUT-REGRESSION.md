# Hero grid regression

On 30/10/2026 the local dashboard's binding-facts panel consumed the entire
hero row. A managed server-browser measurement at 1919 CSS pixels found the
verdict and detail **0 pixels wide** and the hero **1946.69 pixels tall**.
The stylesheet loaded successfully and the DOM was intact.

Hypothesis confirmed by changing only the facts track: its unbounded `auto`
max-content size starved `minmax(0, 1fr)`. `fit-content(60%)` preserves natural
width for short facts while leaving room for the verdict. Existing narrow-screen
stacking and the no-binding desktop layout are unchanged.

## Regression checks

- `pytest tests/test_ui_layout.py`: 25 passed, including a source-contract test
  that failed before the CSS correction.
- `pytest tests/test_ui*.py`: 217 passed.
- The managed NixOS-server browser rendered the real stylesheet and current
  hero markup in isolated iframe viewports at 195, 390, 768, 1024, 1280 and
  1919 CSS pixels, both with and without binding facts: **12/12 passed**.
  Every verdict retained usable width; no horizontal overflow occurred.
- At 1919 pixels with binding facts, the corrected fixture's verdict width
  was 731 pixels and hero height 93.75 pixels. The single-variable live-page
  probe likewise reduced the hero to 95.75 pixels (724.59-pixel verdict).

These browser probes demonstrate the CSS fix; they do not themselves deploy
it. Verify the served asset revision and rendered geometry after activation.
Runtime/private browser receipts live in the Throttler vault project's
`evidence/2026-10-30-hero-width-browser.json`.

No account selection, quota interpretation, forwarding or limiter behavior is
changed. Reversal is one revert of the UI fix commit.

## Accepted on the live dashboard — 02/11/2026

Status: **ACCEPTED**. The measurements below come from one run against the
running gateway (`GET /ui`), not from a fixture: build
`fv5rk1m5j2vjg7790mk4wa7mjb3wjnag-throttler-gateway-0.1.0`, served stylesheet
`/ui/static/style.css?v=6cfa81d3b314`, script exit 0, **12/12 assertions PASS**.
The regression numbers above stay as the baseline these are read against.

| Measurement | Accepted value | Boundary | Regression baseline |
|---|---|---|---|
| hero height at 1919 CSS px | **68.5 px** | <= 200 px | 1946.69 px |
| `.hero-verdict` width | **1022.11 px** | > 0 | 0 px (731 px fixture / 724.59 px probe) |
| `.hero-detail` width | **746.3 px** | > 0 | 0 px |
| horizontal overflow | **none**, scrollWidth 1919 == clientWidth 1919 | equal | - |
| facts track | **826.875 px = 43.8 %** of the 1888.98 px hero | <= 60 % (`fit-content(60%)`) | unbounded max-content |
| 200 % zoom, hero height | **111.38 px** at 960x496 CSS px, DPR 2 | holds | - |
| 200 % zoom, verdict / detail | **310.36 px / 310.36 px** | > 0 | - |
| 200 % zoom, overflow | **none**, scrollWidth 945 == clientWidth 945 | equal | - |

Geometry measured live (CSS px): viewport 1919x992 at DPR 1; hero
1888.98x68.5 px, grid tracks `1022.11px 826.875px`; verdict word "PACING" is
84.97 px on its own, which is the text width, not the container.

### Acceptance method (word for word)

The managed NixOS-server browser fleet through the supported `interactive.attach`
CDP path: identity `brave-search` (host Nix.Server, headed, tunneled),
attach-only (`start_if_down=False`, opens a fresh tab), the page driven by
Playwright over CDP. No local browser is started and no cookies are copied.

Re-run, from the evidence directory
`1. Projects/Throttler/evidence/2026-11-02-visual-acceptance/` in the Throttler
vault project (the script lives there as `hero_accept.py`):

```sh
browser-patchright hero_accept.py
```

It writes `measurements.json`, `dashboard-1x-full.png`,
`dashboard-200pct-full.png` and the hero crops, prints one PASS/FAIL line per
assertion, and exits 0 only when all of them pass. `--web-repo` defaults to the
web-automation checkout that provides `lib.interactive`. The 200 % level is
Chrome page zoom as layout sees it: CDP `Emulation.setDeviceMetricsOverride`
halves the CSS viewport and doubles the device scale factor at the same
physical window, so media queries reflow exactly as they do under `Ctrl+`.

### Paths that are INVALID for future attempts

- **Do NOT launch a local patchright/Playwright browser and do NOT serve a
  synthetic origin** (`page.set_content`, a routed `http://preview.test`, or a
  temp server on this desktop's loopback). Those attempts were **REFUSED** by
  the `STEALTH_DOMAINS` server-only guard, and the refusal is the answer, not a
  hurdle to route around. This is the path that stalled the work.
- **Do NOT synthesize keyboard zoom** (`Control+Equal` through CDP input):
  measured as ineffective, because page zoom is browser-chrome state, not page
  state. Use the device-metrics override above.
- **Do NOT substitute curl/WebFetch** for a rendered measurement: an HTTP 200
  says nothing about geometry.

The server-fleet route is the only valid one. If the fleet, the identity or the
attach path is unavailable, record the exact refusal and stop - an honest hold
beats a fabricated PASS.
