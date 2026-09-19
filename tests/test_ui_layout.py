"""The dashboard's hierarchy is a rule, not a taste — so it is a test.

Written 18/09/2026, after a computed-style census of the live panel found NINE
distinct font sizes between 8.7px and 11.9px (8.7, 9.57, 10.44, 10.73, 11.31,
11.6, 11.89), a 1.37x spread carrying no hierarchy at all. The panel title was
*smaller* than its own column headers (10.73 vs 11.31) and smaller again than
the row identities under it (11.6): every level of the document was the same
size as every other. `docs/DASHBOARD-DESIGN.md` had named this ("uniform
typographic weight") as the reason the page read as machine-generated, and it
had survived two passes because nothing enforced it.

Purely-CSS defects are still regressions, and a browser is not needed to catch
the two that keep coming back:

  * a size written by hand, six months after the scale was agreed;
  * a grid that opens more tracks than the page has panels, which on a 2560px
    screen draws two of them and re-creates the dead third of the viewport that
    the two-column shell exists to remove (measured: `repeat(auto-fit,
    minmax(34rem, 1fr))` opened FOUR tracks at 2560px).

What this file cannot check is whether the result reads well. That is what the
screenshots at 390/768/1440/2210/2560 in the PR are for, and what
`tests/render_preview.py` exists to produce.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The renderer itself is shared with `test_ui_status.py`; see `tests/ui_render.py`.
from ui_render import render_stats as _render

_ROOT = Path(__file__).resolve().parents[1]
_CSS = _ROOT / "src/anthropic_throttle_proxy/ui/static/style.css"
_STATS = _ROOT / "src/anthropic_throttle_proxy/ui/templates/partials/stats.html"
_DASH = _ROOT / "src/anthropic_throttle_proxy/ui/templates/dashboard.html"

# The scale, in the order a document should rank them. One token per rank, and
# every `font-size` on the page resolves to one of them.
SCALE = ("--fs-hero", "--fs-value", "--fs-title", "--fs-body", "--fs-meta", "--fs-head", "--fs-tag")
# The single literal: the root size the rem scale is defined against.
ROOT_LITERAL = "15px"


def _css() -> str:
    return _CSS.read_text(encoding="utf-8")


def _declared(prop: str) -> list[str]:
    """Every value declared for ``prop``, in source order."""
    return [v.strip() for v in re.findall(rf"(?m)(?<![\w-]){prop}:\s*([^;{{}}]+)", _css())]


def test_every_font_size_comes_from_the_scale():
    """No hand-written size anywhere — the census is the reason this pass exists.

    Covers the `font:` SHORTHAND as well as `font-size`. The shorthand resets
    every unspecified typographic property and carries a size, and one
    `.hero .verdict { font: 9px monospace; }` slipped past the first version of
    this test (cross-family review, 18/09/2026).
    """
    values = _declared("font-size")
    assert values, "no font-size declarations found — did the stylesheet move?"
    allowed = {f"var({token})" for token in SCALE} | {ROOT_LITERAL}
    offenders = sorted({v for v in values if v not in allowed})
    assert not offenders, (
        "font-size values outside the scale: "
        f"{offenders}. Add a rank to :root and use it, or the page drifts back "
        "to a spread that carries no hierarchy."
    )
    for shorthand in _declared("font"):
        assert "px" not in shorthand and "rem" not in shorthand, (
            f"`font: {shorthand}` carries a size and bypasses the scale"
        )


def test_the_scale_is_strictly_ordered_and_every_rank_is_used():
    """A rank nothing uses is a rank that will be re-invented by hand."""
    css = _css()
    sizes = {}
    for token in SCALE:
        match = re.search(rf"{token}:\s*([0-9.]+)rem", css)
        assert match, f"{token} is in the scale list but not declared in :root"
        sizes[token] = float(match.group(1))
    ordered = [sizes[t] for t in SCALE]
    assert ordered == sorted(ordered, reverse=True), f"scale is not monotonic: {sizes}"
    assert len(set(ordered)) == len(ordered), f"two ranks share a size: {sizes}"
    for token in SCALE:
        assert f"var({token})" in css, f"{token} is declared but never used"


def test_root_literal_is_the_only_px_size():
    """`font-size: 13px` on one component is how a scale dies."""
    px = [v for v in _declared("font-size") if v.endswith("px")]
    assert px == [ROOT_LITERAL], f"unexpected px font sizes: {px}"


def _rule_size(selector: str) -> str:
    """The `font-size` token declared by the rule whose selector matches."""
    match = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", _css(), re.S)
    assert match, f"no rule found for {selector!r}"
    size = re.search(r"font-size:\s*([^;]+);", match.group(1))
    assert size, f"{selector!r} does not declare a font-size"
    return size.group(1).strip()


def test_the_rendered_hierarchy_is_what_the_scale_promises():
    """Compare the RULES, not the token definitions.

    The first version of this test compared `--fs-title` against `--fs-head` in
    `:root`, which passes just as happily while `h2 { font-size: var(--fs-tag) }`
    reverses the hierarchy the tokens describe (cross-family review,
    18/09/2026). These are the four rules that decide what the page actually
    renders at each rank.
    """
    rank = {
        "hero verdict": _rule_size(".hero .verdict"),
        "panel title": _rule_size("h2"),
        "column header": _rule_size("table.bearers thead th"),
        "row identity": _rule_size(".sub-ident .acct"),
        "meter label": _rule_size(".meter-label"),
    }
    assert rank["hero verdict"] == "var(--fs-hero)"
    assert rank["panel title"] == "var(--fs-title)"
    assert rank["column header"] == "var(--fs-head)"
    assert rank["row identity"] == "var(--fs-title)"
    assert rank["meter label"] == "var(--fs-body)"

    css = _css()
    numeric = {
        key: float(re.search(rf"{token[4:-1]}:\s*([0-9.]+)rem", css).group(1))
        for key, token in rank.items()
    }
    assert numeric["hero verdict"] > numeric["panel title"] > numeric["column header"], (
        f"the hero, the panel title and its column headers must descend: {numeric}"
    )
    assert numeric["row identity"] > numeric["meter label"] > numeric["column header"], (
        "a row's name must outrank its meter label, which must outrank a column "
        f"header — the inversion that defined the old page: {numeric}"
    )


@pytest.mark.parametrize(
    "template",
    sorted(
        str(p.relative_to(_ROOT / "src/anthropic_throttle_proxy/ui/templates"))
        for p in (_ROOT / "src/anthropic_throttle_proxy/ui/templates").rglob("*.html")
    ),
)
def test_templates_never_inline_a_font_size(template):
    """A `style="font-size:9px"` in markup bypasses the scale entirely.

    Every template, not the two the first version named, and the `font:`
    shorthand as well — an inline size in a partial nobody thought to list is
    still an inline size.
    """
    src = (_ROOT / "src/anthropic_throttle_proxy/ui/templates" / template).read_text(
        encoding="utf-8"
    )
    assert "font-size" not in src, f"{template} sets a font size inline"
    assert not re.search(r"\bfont\s*:", src), f"{template} sets the font shorthand inline"
    assert "<style" not in src, f"{template} carries a style block"


def _track_count(value: str) -> int:
    """Number of tracks in a `grid-template-columns` value.

    Tracks are separated by TOP-LEVEL WHITESPACE, not by commas — the commas
    live inside `minmax(...)` and `repeat(...)`. The first version of this
    helper counted commas, so it read `1fr 1fr 1fr 1fr` and
    `minmax(0, 1.3fr) minmax(40rem, 1fr)` as the same answer, and an in-memory
    four-column shell passed the test that exists to catch it (cross-family
    review, 18/09/2026).
    """
    text = value.strip()
    repeated = re.fullmatch(r"repeat\((\d+)\s*,\s*(.+)\)", text, re.S)
    if repeated:
        return int(repeated.group(1)) * _track_count(repeated.group(2))
    depth = 0
    count = 1
    for char in text:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char.isspace() and depth == 0:
            count += 1
    return count


def test_the_track_counter_counts_what_it_claims_to():
    """Guard the guard: this helper shipped once already and read every grid as
    one column."""
    assert _track_count("minmax(0, 1fr)") == 1
    assert _track_count("minmax(0, 1.3fr) minmax(40rem, 1fr)") == 2
    assert _track_count("1fr 1fr 1fr 1fr") == 4
    assert _track_count("repeat(3, minmax(9rem, 1fr))") == 3


def test_the_shell_opens_exactly_two_columns():
    """Guards the auto-fit regression that drew two panels across four tracks."""
    shell_blocks = re.findall(r"\.shell\s*\{([^}]*)\}", _css())
    assert shell_blocks, "no .shell rule found"
    for block in shell_blocks:
        tracks = re.search(r"grid-template-columns:\s*([^;]+);", block)
        if not tracks:
            continue
        value = tracks.group(1)
        assert "auto-fit" not in value and "auto-fill" not in value, (
            "the shell must declare its two columns outright: an intrinsic track "
            f"count opens more tracks than the page has panels ({value})"
        )
        count = _track_count(value)
        assert count <= 2, f"the shell opened {count} tracks: {value}"


def test_panels_fill_their_cell_instead_of_shrinking_to_fit():
    """`fit-content` panels produced the ragged right edge this pass removed.

    Measured before: the capacity panel ended 766px short of the viewport, the
    providers panel 1211px short, the bearers panel 1423px short.
    """
    block = re.search(r"\.bearers-wrap\s*\{([^}]*)\}", _css()).group(1)
    assert "width: 100%" in block, ".bearers-wrap must claim its cell"
    assert "fit-content" not in block, ".bearers-wrap must not shrink to fit"
    table = re.search(r"table\.bearers\s*\{([^}]*)\}", _css()).group(1)
    assert "width: 100%" in table, "table.bearers must fill its panel"
    assert "min-width: 62rem" not in table and "min-width: 64rem" not in table, (
        "a table minimum wider than its cell forces the panel to scroll on the "
        "very screens the two-column shell was added for"
    )


def test_the_disconnect_cue_is_css_only_and_survives_reduced_motion():
    """The one animation on the page that IS the information.

    `#stats` is re-rendered every 2s, so `.as-of` is a new element every 2s and
    its animation restarts with it: leave the page alone and the animation runs
    out, at which point the stamp turns amber and says the readings are last
    known. The obvious "helpful" refactor — zeroing animation durations under
    `prefers-reduced-motion` — would delete the signal for exactly the users
    who asked for less motion, so the exemption is asserted rather than left to
    survive by luck.
    """
    css = _css()
    cue = re.search(r"\.as-of\s*\{([^}]*)\}", css)
    assert cue, "no .as-of rule"
    assert "animation:" in cue.group(1), "the staleness cue must be a CSS animation"
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*)\n\}", css, re.S)
    assert reduced, "the reduced-motion block is gone"
    body = reduced.group(1)
    assert re.search(r"\.as-of[^{]*\{[^}]*animation-duration:\s*6s\s*!important", body), (
        "the staleness cue needs an explicit reduced-motion exemption: it is "
        "read-only information that happens to be delivered by a duration"
    )
    # And no script crept in to replace it.
    assert "<script" not in _STATS.read_text(encoding="utf-8")


def test_hero_carries_the_verdict_the_binding_and_the_way_out_in_one_panel():
    """The page's first panel answers all three questions, or it does not.

    They used to be a thin status strip, a separate conditional strip, and a
    clause inside a sentence that named a bearer hash. An operator reading the
    top of the page should not have to join three objects to learn what is
    blocking the fleet.
    """
    html = _render(
        status={
            "level": "pacing",
            "verdict": "PACING",
            "since": "23m",
            "detail": "2 of 4 bearers pacing",
            "binding": {
                "subscription": "pedro@pm.me",
                "sub": "pedro@pm.me",
                "window": "7d",
                "pct": 94,
                "resets_in": "2d 04h",
                "next_usable": "pedro@proton.me",
                "next_usable_pct": 38,
                # The measured reason. Only an upstream REFUSAL earns the word
                # "blocked" (cross-family review, 18/09/2026); this fixture is
                # the throttled case, so it says so.
                "evidence": "throttled",
            },
        }
    )
    hero = re.search(r'<section class="hero[^"]*".*?</section>', html, re.S)
    assert hero, "no hero panel rendered"
    panel = hero.group(0)
    for fact in ("PACING", "23m", "blocked", "pedro@pm.me", "94%", "reopens in 2d 04h"):
        assert fact in panel, f"{fact!r} is not in the hero panel"
    assert "takes traffic next" in panel and "pedro@proton.me" in panel
    assert panel.index("blocked") < panel.index("takes traffic next"), (
        "the facts read in the order the question is asked"
    )


def test_the_hero_says_pacing_when_nothing_was_refused():
    """`_compute_status` builds a binding for PACING too. Queue pressure is not
    a quota refusal, and the hero must not publish one as the other."""
    html = _render(
        status={
            "level": "pacing",
            "verdict": "PACING",
            "since": "4m",
            "detail": "",
            "binding": {
                "subscription": "A",
                "sub": "a@example.test",
                "window": "7d",
                "pct": 30,
                "resets_in": "1h",
                "evidence": "pacing",
            },
        }
    )
    panel = re.search(r'<section class="hero[^"]*".*?</section>', html, re.S).group(0)
    assert "<dt>binding constraint</dt>" in panel
    assert "<dt>blocked</dt>" not in panel
    assert "resets in 1h" in panel and "reopens in" not in panel


def test_the_hero_distinguishes_unverified_from_blocked_siblings():
    """Both used to render "nothing — every sibling is blocked too", which is a
    claim the page could only make for one of them."""
    base = {
        "level": "throttled",
        "verdict": "THROTTLED",
        "since": "2m",
        "detail": "",
        "binding": {
            "subscription": "A",
            "sub": "a@example.test",
            "window": "7d",
            "pct": 100,
            "resets_in": "1h",
            "evidence": "throttled",
        },
    }
    unverified = dict(base)
    unverified["binding"] = {**base["binding"], "next_usable_unknown": True}
    html = _render(status=unverified)
    panel = re.search(r'<section class="hero[^"]*".*?</section>', html, re.S).group(0)
    assert "no verified alternative" in panel
    assert "every sibling is blocked too" not in panel

    html = _render(status=dict(base))
    panel = re.search(r'<section class="hero[^"]*".*?</section>', html, re.S).group(0)
    assert "every sibling is blocked too" in panel
    assert "no verified alternative" not in panel


def test_hero_omits_the_facts_block_when_nothing_is_binding():
    """No binding → no empty labelled cells pretending there is one."""
    html = _render(status={"level": "healthy", "verdict": "HEALTHY", "since": "3h", "detail": ""})
    hero = re.search(r'<section class="hero[^"]*".*?</section>', html, re.S).group(0)
    assert "HEALTHY" in hero
    assert "blocked" not in hero and "takes traffic next" not in hero


def test_every_meter_track_can_shrink_instead_of_overflowing():
    """A grid track that bottoms out at max-content overflows its cell.

    Measured live 18/09/2026, on the installed package: the Z.AI row's
    `resets 2h 17m 19/09/2026 02:48 UTC` — the absolute stamp #227 added beside
    the countdown — overflowed its cell by 50px and painted into the `7d` label
    of the meter beside it. It needed a browser to see, because the page-level
    overflow probe deliberately skips panel interiors (those scroll on purpose),
    but the CAUSE is checkable at source: a `auto` track bottom-limits at
    max-content, so anything long enough makes the row wider than its cell.
    """
    block = re.search(r"\n\.meter\s*\{([^}]*)\}", _css()).group(1)
    tracks = re.search(r"grid-template-columns:\s*([^;]+);", block).group(1)
    for track in re.findall(r"minmax\([^)]*\)|[^\s(]+", tracks):
        if track.startswith("minmax("):
            minimum = track[len("minmax(") : -1].split(",")[0].strip()
            assert minimum == "0" or minimum.endswith(("rem", "%")), (
                f"track {track!r} cannot shrink below a content minimum"
            )
        else:
            assert track != "auto", (
                f"track {track!r} bottom-limits at max-content, so a long value "
                "overflows the cell instead of wrapping inside it"
            )

    # ...and the wrappers have to be allowed to wrap, or narrowing the track
    # only moves the overflow.
    state = re.search(r"\n\.meter-state\s*\{([^}]*)\}", _css()).group(1)
    assert "flex-wrap: wrap" in state, ".meter-state must wrap"
    assert "min-width: 0" in state, ".meter-state must be allowed to shrink"
    reset = re.search(r"\n\.reset-in\s*\{([^}]*)\}", _css()).group(1)
    assert "min-width: 0" in reset, ".reset-in must be allowed to shrink"
    assert "nowrap" not in reset, ".reset-in must wrap rather than overflow"


def test_no_caption_is_truncated_and_no_email_can_outrun_its_column():
    """Two live clipping defects from 18/09/2026, both invisible to the page
    probe (they are inside a panel, or inside `text-overflow`).

    * `.gauge-detail` was `nowrap` + ellipsis, so `(in-flight + queued) ÷ live
      cap` — the definition of what the saturation tile measures — lost 54px of
      itself at every width up to 1180px. A caption that says which quantity a
      number is cannot be the thing that gets dropped.
    * `phsb5321@gmail.com` has no break opportunity, so it painted 22px past its
      column and over the `(C)` credential tag at 1600 AND 1920px.
    """
    detail = re.search(r"\n\.gauge-detail\s*\{([^}]*)\}", _css()).group(1)
    assert "text-overflow" not in detail, "a gauge caption must not be truncated"
    assert "nowrap" not in detail, "a gauge caption must be allowed to wrap"

    account = re.search(r"\ncol\.c-tok-acct\s*\{\s*width:\s*([0-9.]+)rem", _css())
    assert account, "the token table's account column lost its width"
    assert float(account.group(1)) >= 12, (
        "an 18-character address plus its credential tag needs at least 12rem, "
        "or it overflows the cell (measured: 9rem clipped by 22px)"
    )
    css = _css()
    assert re.search(r"table\.bearers\.tokens td:nth-child\(2\)\s*\{[^}]*overflow-wrap", css), (
        "the account cell must be allowed to break a long address"
    )
