# Plan: UI Quality Fixes (Spec 032)

**Plan date**: 2026-05-27
**Spec**: [spec.md](spec.md)
**Implementation commit**: `1fd61b5` on `031-ui-fixes` (PR #32)

---

## Technical context

- **Stack**: Python 3.13 + aiohttp + aiohttp-jinja2 + HTMX 1.9.12 (CDN-loaded, integrity-pinned)
- **CSS**: Pure CSS3, no framework, Catppuccin Mocha palette tokens via CSS custom properties on `:root`
- **No JS**: Server-rendered HTML, HTMX attribute-driven interactivity only
- **Tests**: `pytest` + `pytest-asyncio` (90 tests in suite)
- **Lint**: `ruff` (Python only — no CSS/HTML linter wired)

## Constitution gates

| Principle | Evidence |
|---|---|
| I. No vendor AI SDK on hot path | UI changes don't touch hot path; advisor still lazy-imported via `_maybe_advise` + `ui.routes.advisor` |
| II. Bearer hash never raw token | UI renders `b.bearer_id` which is the 8-char sha256 prefix from `_bearer_id` |
| III. AIMD floor ≥1 | Untouched; clamp at `config.py:66` from PR #30 holds |
| IV. /__throttle/health <50 ms | Untouched |
| V. THROTTLE_UPSTREAM only redirect | Untouched |
| VI. HTMX 1.x no JS modules | Re-verified: dashboard.html has ONE `<script>` (HTMX CDN), zero ESM/Alpine/React, server-rendered partials |

## Anti-AI-slop gate (per ~/.claude/rules/anti-ai-slop.md)

1. ❌ Indigo→violet→pink gradient hero — N/A, dashboard has no gradient
2. ❌ 100vh hero — dashboard has no hero
3. ❌ 3-column rounded-card feature grid — N/A
4. ❌ Lucide/Heroicons AI-starter-pack — only one favicon SVG
5. ❌ Glassmorphism — no `backdrop-blur` anywhere
6. ❌ Inter as only typeface — fonts are `ui-sans-serif`/`system-ui` + JetBrains Mono fallback
7. ❌ Dark mode = pure black + neon glow — base is `#1e1e2e` (Catppuccin), no neon shadows
8. ❌ Vertical accent borders on every callout — only on status strip + advisor (intentional, not on every block)
9. ❌ Sparkle emoji / shimmer animations — none
10. ❌ Bounce easing — only `120ms ease-out` in transitions
11. ❌ Lorem ipsum / placeholder content — all copy is real
12. ❌ Stock testimonial blocks — N/A
13. ❌ Skipped heading hierarchy — h1 (brand) → h2 (sections) → h3 (advisor output) — sequential
14. ❌ Cramped padding under generous margin — `padding: 0.85rem 1.1rem` on cards, balanced
15. ❌ Symmetric pricing table — N/A
16. ❌ One radius/shadow scale across all surfaces — table has `0` radius, cards have `8px`, inputs have `3px`, buttons have `6px` — each surface class has its own. PASS.

## Phase 0 — Research (already done)

- Verified HTMX 1.x non-2xx default behaviour: confirmed via the spec source (https://htmx.org/docs/#response-handling) — non-2xx responses are NOT swapped unless `htmx.config.responseHandling` is overridden OR the response carries `HX-Retarget`/`HX-Reswap` headers OR the user sets `hx-swap-error`. Cheapest fix is to return 200 with rendered HTML partial.

## Phase 1 — Design artifacts

**Affected surfaces:**

| File | Change |
|---|---|
| `ui/templates/dashboard.html` | settings `<details open>`, advisor section unconditional, scroll-into-view swap, empty-state with EnvironmentFile path |
| `ui/templates/partials/stats.html` | `<colgroup>` with col-bearer/col-num/col-aimd/col-window, AIMD `td-center`, tooltips on req-left/retry-after/clients |
| `ui/templates/partials/advisor.html` | error-vs-success branching, snapshot details kept |
| `ui/static/style.css` | table-layout:fixed, col widths, td-center, advisor-output.err, advisor-empty, mobile @media |
| `ui/routes.py` | advisor() returns 200 with rendered partial on all paths |
| `config.py` | every EDITABLE_KNOBS entry expanded to ≥3 sentences |
| `tests/test_proxy_app.py` | `test_ui_advisor_disabled_renders_inline_error` expects 200 + HTML |
| `tests/test_forwarding_paths.py` | `test_ui_advisor_enabled_surfaces_error` expects 200 + HTML |

## Phase 2 — Tasks

See [tasks.md](tasks.md). 12 tasks (T001-T012), 1 Codex review (T013), 1 anti-slop audit (T014), 1 merge (T015).
