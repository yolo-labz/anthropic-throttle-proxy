# Capacity records — bounded Operate-mode slice

## Scope and hypothesis

The capacity table reserves 21rem for identity and 11rem for burn below the
1280px overview breakpoint. Hypothesis: on narrow screens those fixed columns
crowd out the meter column. Reflow each subscription below 900 CSS pixels into
one labelled record: account and verdict, meters, then burn. Above that width
the existing three-column layout is unchanged.

This is a **candidate fix, not visually accepted**. The hypothesis is falsified
by panel/page overflow, unreadable meters, or lost account/verdict associations
in the rendered geometry and screenshots. Source contracts alone cannot prove
this acceptance.

No factual status/quota copy, provider projection, routing, live YAML, header
controls, polling, motion, or hero track sizing changes. Catppuccin tokens and
the native table headers remain; an explicit table role preserves semantics
when CSS changes the layout. Visible field labels apply only to narrow records.

## Implementation / test plan

1. Add negative source regressions: visible record field labels and bounded
   one-column reflow. Both failed before the candidate CSS/template changes.
2. Add the narrow-only CSS and presentation labels, preserving native DOM order.
3. Run UI/layout tests, lint/format and the single-script/freshness contract.
4. Run before/after rendered checks on the exact source assets, inspect the
   images, then validate real HTMX polling, focus and scroll separately.

Steps 1–3 passed locally (27 layout tests; 219 UI tests). Step 4 is blocked by
the supported screenshot driver's policy-verification refusal, not waived.

## Reproducible rendered check (pending)

Use the registered, healthy anonymous server identity only. Do not launch a
local browser or retry around a fleet refusal. After the supported driver is
repaired, run from the repository with its source on PYTHONPATH:

```sh
PYTHONPATH="$PWD/src" .venv/bin/python tests/render_preview.py /durable/path/ui.html
/path/to/web-automation/.venv/bin/python tests/impeccable_capacity_browser.py \
  --html /durable/path/ui.html \
  --css src/anthropic_throttle_proxy/ui/static/style.css \
  --out /durable/path/browser-result \
  --web-repo /path/to/web-automation
```

The existing preview is synthetic layout data, **not today's provider inventory**.
The browser check uses the actual Jinja-rendered markup and supplied CSS, saves
its CSS digest, geometry and screenshots at 195/390/768/1280/1919 CSS pixels,
and exercises the detail checkbox by keyboard with reduced-motion enabled.
195px is a narrow reflow stress proxy for a 390px viewport at 200% zoom; it is
not physical-device or native-browser-zoom evidence. No static fixture test
proves HTMX polling: real refresh/focus/scroll and actual 200% zoom acceptance
remain explicitly pending, as does visual inspection.

## Delivery boundary

Do not activate a production package for this slice. Do not call a draft PR
accepted or merge it until the required rendered acceptance is obtained.
Reversal: one revert PR for this UI-only commit. No data migration or runtime
rollback is required because no runtime change was authorized.

## Resume verdict (30/10/2026)

Corrected candidate: main dcb6c8a + the 18 capacity lines (`git merge-file`; the earlier overlay that deleted the per-project block is superseded). Reflow passes at 195/390/768/1280/1919 CSS px. The hero verdict is readable at each width (≥120px, unclipped, ≥10px). The former 80px floor measured `.verdict`, the status pill, and is retired. Collapsed-hero negative control fails. Keyboard focus passes. Not accepted: real HTMX state retention and native 200% zoom are untested; narrower CSS px is reflow, not browser zoom. Evidence: `evidence/blocker-clearance-2026-10-30/visual-result.md`.

## Final status (30/10/2026)

Not accepted and not merged. Real HTMX state retention is blocked by the supported browser's fail-closed STEALTH_DOMAINS goto-gate on the synthetic `preview.test` origin; the gate bypass was not used. Native 200% zoom via keyboard is not effective in the remote browser. Reflow at narrower CSS px remains reflow, not browser zoom. Evidence: `evidence/blocker-clearance-2026-10-30/visual-result.md`.
