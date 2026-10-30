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
