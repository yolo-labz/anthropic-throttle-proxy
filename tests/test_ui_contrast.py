"""WCAG 1.4.3 contrast gate for the dashboard's text tokens.

The repo has no browser in CI and does not need one for this: every text colour
on ``/ui`` is a CSS custom property, and every background it lands on is one of
three surfaces. Resolving those pairs and computing the WCAG contrast ratio is
pure arithmetic, so the rule that a live axe run measures is enforceable here at
pytest speed.

Found by axe-core 4.12.1 on 05/08/2026: ``--muted`` was ``--ctp-overlay0``
(#6c7086) and failed AA on 23 nodes — 3.36:1 on base, 2.57:1 on surface0 — on
the 9px labels that carry every column header and unit on the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CSS = Path(__file__).resolve().parents[1] / "src/anthropic_throttle_proxy/ui/static/style.css"

# WCAG 2.1 AA for text below 18.66px (or below 24px when not bold), which is
# every one of these tokens' real usages — the dashboard's largest --muted text
# is 0.72rem.
AA_NORMAL_TEXT = 4.5

# Backgrounds a text token can land on, in the order the page stacks them.
SURFACES = ("--ctp-base", "--ctp-mantle", "--ctp-surface0")

# Table rows are not opaque surfaces: they are `color-mix(... , transparent)`
# composited onto the panel. #216 moved the panel to mantle and the zebra to
# crust, which changes every ratio measured above — so the composited results
# are gated here too, rather than assumed from the opaque cases.
COMPOSITED_ROWS = {
    # (top colour, alpha, backdrop) -> the rule in style.css it comes from
    "zebra (odd row on the panel)": ("--ctp-crust", 0.55, "--ctp-mantle"),
    "row hover": ("--ctp-surface0", 0.55, "--ctp-mantle"),
    "binding row": ("--crit", 0.07, "--ctp-mantle"),
}

# Tokens used as TEXT colour anywhere in style.css. Decorative-only tokens
# (dots, hairlines, bar fills) are deliberately absent: a 3:1 non-text token is
# not a 1.4.3 failure, and demanding 4.5 there would force the palette flat.
TEXT_TOKENS = (
    "--ctp-text",
    "--ctp-subtext0",
    "--ctp-subtext1",
    "--muted",
    "--ok",
    "--warn",
    "--crit",
    "--info",
    "--accent",
)


def _declarations() -> dict[str, str]:
    """Every ``--name: value`` in the :root block, values unresolved."""
    css = _CSS.read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert root, "style.css has no :root token block"
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root.group(1))
    }


def _resolve(token: str, decls: dict[str, str], depth: int = 0) -> str:
    """Follow ``var(--alias)`` chains down to a literal hex colour."""
    assert depth < 10, f"{token} does not resolve to a literal"
    value = decls[token]
    alias = re.fullmatch(r"var\((--[\w-]+)\)", value)
    return _resolve(alias.group(1), decls, depth + 1) if alias else value


def _relative_luminance(hex_colour: str) -> float:
    """WCAG relative luminance of an ``#rrggbb`` colour."""
    raw = hex_colour.lstrip("#")
    assert len(raw) == 6, f"expected #rrggbb, got {hex_colour!r}"
    channels = []
    for offset in (0, 2, 4):
        srgb = int(raw[offset : offset + 2], 16) / 255
        channels.append(srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio between two ``#rrggbb`` colours."""
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast_math_matches_the_known_reference_pairs():
    """Guard the maths itself, so a wrong formula cannot pass the real gate."""
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#1e1e2e", "#1e1e2e") == pytest.approx(1.0, abs=0.001)
    # The exact failure axe reported for the old --muted on base.
    assert contrast_ratio("#6c7086", "#1e1e2e") == pytest.approx(3.36, abs=0.02)


@pytest.mark.parametrize("token", TEXT_TOKENS)
@pytest.mark.parametrize("surface", SURFACES)
def test_text_token_meets_wcag_aa_on_every_surface(token: str, surface: str):
    decls = _declarations()
    foreground = _resolve(token, decls)
    background = _resolve(surface, decls)
    ratio = contrast_ratio(foreground, background)
    assert ratio >= AA_NORMAL_TEXT, (
        f"{token} ({foreground}) on {surface} ({background}) is {ratio:.2f}:1, "
        f"below the WCAG 1.4.3 AA floor of {AA_NORMAL_TEXT}:1 for normal-size text"
    )


def _composite(top: str, alpha: float, backdrop: str) -> str:
    """Flatten ``color-mix(in srgb, top alpha%, transparent)`` over a backdrop.

    sRGB source-over, which is what a browser paints for a translucent row
    background sitting on an opaque panel.
    """
    top_raw, back_raw = top.lstrip("#"), backdrop.lstrip("#")
    channels = []
    for offset in (0, 2, 4):
        over = int(top_raw[offset : offset + 2], 16)
        under = int(back_raw[offset : offset + 2], 16)
        channels.append(round(over * alpha + under * (1 - alpha)))
    return "#" + "".join(f"{c:02x}" for c in channels)


def test_composite_math_matches_its_endpoints():
    """Guard the compositor before trusting it in the gate below."""
    assert _composite("#ffffff", 1.0, "#000000") == "#ffffff"
    assert _composite("#ffffff", 0.0, "#000000") == "#000000"
    assert _composite("#ffffff", 0.5, "#000000") == "#808080"


@pytest.mark.parametrize("token", TEXT_TOKENS)
@pytest.mark.parametrize("row", sorted(COMPOSITED_ROWS))
def test_text_token_meets_wcag_aa_on_every_composited_row(token: str, row: str):
    decls = _declarations()
    top, alpha, backdrop = COMPOSITED_ROWS[row]
    background = _composite(_resolve(top, decls), alpha, _resolve(backdrop, decls))
    foreground = _resolve(token, decls)
    ratio = contrast_ratio(foreground, background)
    assert ratio >= AA_NORMAL_TEXT, (
        f"{token} ({foreground}) on the {row} background ({background}) is "
        f"{ratio:.2f}:1, below the WCAG 1.4.3 AA floor of {AA_NORMAL_TEXT}:1"
    )


def test_every_text_token_is_declared():
    """A renamed token must fail loudly here, not silently stop being checked."""
    decls = _declarations()
    composited = [part for top, _, backdrop in COMPOSITED_ROWS.values() for part in (top, backdrop)]
    missing = [t for t in (*TEXT_TOKENS, *SURFACES, *composited) if t not in decls]
    assert not missing, f"tokens missing from :root: {missing}"
