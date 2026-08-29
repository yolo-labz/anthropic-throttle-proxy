"""Every icon the dashboard emits must have a font that can draw it.

Regression for #215: provider, meter and status icons were added inside spans
that inherit ``--mono`` / ``--sans``. Both stacks ended at the ``monospace`` /
``sans-serif`` generic, and the fallback that resolves carries no colour-emoji
glyph, so the whole icon set rendered as tofu boxes. The icons are not
decoration — they are the only per-row provider cue in the subscriptions
table, so a stack without an emoji family is a broken dashboard.

Scope, stated honestly: this is a static check that the stacks NAME a font able
to carry these code points. It cannot prove a glyph rasterises — that needs a
browser, which CI does not have (same reasoning as tests/test_ui_contrast.py).
What it does prove is the exact thing that broke: icons reaching a stack whose
last entry is a bare generic.
"""

from __future__ import annotations

import re
import unicodedata

from anthropic_throttle_proxy.ui import routes

CSS = (routes._STATIC / "style.css").read_text(encoding="utf-8")
TEMPLATES = sorted(routes._TEMPLATES.rglob("*.html"))

# The families a browser can actually resolve a colour-emoji glyph from. One is
# enough; the point is that the list does not stop at the bare generic.
EMOJI_FAMILIES = ("Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji")

# The stacks every icon-bearing span inherits.
ICON_STACKS = ("mono", "sans")


def _stack(name: str) -> str:
    match = re.search(rf"^\s*--{name}:\s*(.+?);\s*$", CSS, re.MULTILINE)
    assert match, f"--{name} is not declared in style.css"
    stack = match.group(1)
    # Resolve one level of custom-property indirection (both stacks end in
    # `var(--emoji)`), because that is what the browser does too.
    for ref in re.findall(r"var\(--([\w-]+)\)", stack):
        inner = re.search(rf"^\s*--{ref}:\s*(.+?);\s*$", CSS, re.MULTILINE)
        assert inner, f"--{name} references undeclared --{ref}"
        stack = stack.replace(f"var(--{ref})", inner.group(1))
    return stack


def _pictographs(text: str) -> set[str]:
    """Code points a text font is not expected to carry.

    ``So`` (symbol, other) above U+2100 covers every icon this dashboard uses
    without hand-listing them, so an icon added tomorrow is covered without
    editing this module. Variation selectors and ZWJ joiners are deliberately
    not matched: they carry no glyph of their own, and the BASE code point of
    each sequence is what needs a font.
    """
    return {ch for ch in text if unicodedata.category(ch) == "So" and ord(ch) > 0x2100}


def _icons_from_routes() -> dict[str, set[str]]:
    """The icon maps the templates actually render from.

    The templates mostly emit ``{{ s.icon }}`` / ``{{ m.icon }}`` — the literal
    in the HTML is only the *fallback*. A test that reads templates alone would
    keep passing after every real icon changed.
    """
    return {
        name: _pictographs("".join(getattr(routes, name).values()))
        for name in ("_PROVIDER_ICONS", "_STATUS_ICONS", "_METER_ICONS")
    }


def test_both_icon_stacks_name_an_emoji_family():
    for name in ICON_STACKS:
        stack = _stack(name)
        assert any(family in stack for family in EMOJI_FAMILIES), (
            f"--{name} resolves to {stack!r}, which names no emoji family: "
            "every icon in a span inheriting it renders as a tofu box"
        )


def test_emoji_families_come_after_the_generic_not_instead_of_it():
    """Ordering is load-bearing in both directions.

    After the generic, so Latin text stays on the code font and only the
    glyphs the generic cannot draw fall through. But still PRESENT, which is
    the whole fix — the pre-#216 stacks ended at the generic.
    """
    for name, generic in (("mono", "monospace"), ("sans", "sans-serif")):
        stack = _stack(name)
        family = next(f for f in EMOJI_FAMILIES if f in stack)
        assert stack.index(generic) < stack.index(family), (
            f"--{name} puts {family!r} before {generic!r}; the generic must stay "
            "the last resort for ordinary text"
        )


def test_every_icon_the_routes_emit_is_a_pictograph_the_stacks_target():
    """Guards the other direction: that there ARE icons needing this fix.

    Without it the suite would keep passing after the icon set was silently
    dropped, and the font stacks would carry families nothing uses.
    """
    by_map = _icons_from_routes()
    empty = sorted(name for name, icons in by_map.items() if not icons)
    assert not empty, (
        f"{empty} emit no pictographs any more — either the icons were removed "
        "(then delete this module) or the detector stopped matching them"
    )

    # Template fallbacks (`{{ s.icon or "🤖" }}`) render when a map misses, so
    # they need the same font as the map values.
    fallbacks = set()
    for path in TEMPLATES:
        fallbacks |= _pictographs(path.read_text(encoding="utf-8"))
    assert fallbacks, "no template emits a fallback icon; check the detector"


def test_detector_matches_the_icons_actually_in_use():
    """The heuristic is only trustworthy if it catches the real characters.

    Pins one icon per map, including a variation-selector sequence (✳️ is
    U+2733 U+FE0F), so a future change to `_pictographs` that silently stops
    matching them fails here instead of passing vacuously.
    """
    assert _pictographs("✳️") == {"✳"}, "variation-selector sequences must match on their base"
    assert _pictographs("⏱️") == {"⏱"}
    assert _pictographs("📅") == {"📅"}
    # And it must not fire on ordinary text, or "every stack needs emoji" would
    # become vacuously true for any page.
    assert _pictographs("resets in 3h 41m · 94%") == set()
