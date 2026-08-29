"""Every icon the templates emit must have a font that can draw it.

Regression for #215: provider, meter and status icons were added inside spans
that inherit ``--mono`` / ``--sans``. Both stacks ended at the ``monospace`` /
``sans-serif`` generic, which resolves emoji code points through a fallback
with no colour-emoji glyph, so the whole icon set rendered as tofu boxes. The
icons are not decoration — they are the only per-row provider cue in the
subscriptions table, so a stack without an emoji family is a broken dashboard.
"""

from __future__ import annotations

import re
import unicodedata

from anthropic_throttle_proxy.ui import routes

CSS = (routes._STATIC / "style.css").read_text(encoding="utf-8")
TEMPLATES = sorted(routes._TEMPLATES.rglob("*.html"))

# The families a browser can actually resolve a colour-emoji glyph from. One is
# enough; the point is that the stack does not end at the bare generic.
EMOJI_FAMILIES = ("Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji")


def _stack(name: str) -> str:
    m = re.search(rf"^\s*--{name}:\s*(.+?);\s*$", CSS, re.MULTILINE)
    assert m, f"--{name} is not declared in style.css"
    stack = m.group(1)
    # Resolve one level of custom-property indirection (--mono ends in
    # `var(--emoji)`), because that is what the browser does too.
    for ref in re.findall(r"var\(--([\w-]+)\)", stack):
        inner = re.search(rf"^\s*--{ref}:\s*(.+?);\s*$", CSS, re.MULTILINE)
        assert inner, f"--{name} references undeclared --{ref}"
        stack = stack.replace(f"var(--{ref})", inner.group(1))
    return stack


def _emoji_in(text: str) -> set[str]:
    """Code points a text font is not expected to carry.

    ``So`` (symbol, other) plus the regional/pictographic blocks covers the
    icons the templates actually use without hand-listing them, so a new icon
    added tomorrow is covered by this test without editing it.
    """
    return {
        ch
        for ch in text
        if unicodedata.category(ch) == "So" and ord(ch) > 0x2100  # skip ™ © and friends
    }


def test_both_font_stacks_end_in_an_emoji_family():
    for name in ("mono", "sans"):
        stack = _stack(name)
        assert any(fam in stack for fam in EMOJI_FAMILIES), (
            f"--{name} resolves to {stack!r}, which has no emoji family: "
            "every icon in a span inheriting it renders as a tofu box"
        )


def test_templates_only_emit_icons_the_stacks_can_draw():
    # Guards the other direction: the stacks are only correct relative to what
    # the templates emit, so prove the templates do emit icons (otherwise this
    # suite would keep passing after the icons were silently dropped).
    found: dict[str, set[str]] = {}
    for path in TEMPLATES:
        icons = _emoji_in(path.read_text(encoding="utf-8"))
        if icons:
            found[path.name] = icons

    assert found, (
        "no template emits an icon any more — either the icon set was removed "
        "(then delete this module) or the detector stopped matching"
    )
    # And the stacks that render them are the two the templates inherit.
    assert any(fam in _stack("mono") for fam in EMOJI_FAMILIES)
