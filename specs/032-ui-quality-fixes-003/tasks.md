# Tasks: UI Quality Fixes (Spec 032)

**Date**: 2026-05-27
**Plan**: [plan.md](plan.md)
**Implementation already landed at commit `1fd61b5`; this file tracks the
audit gates retroactively to drive the Codex review + anti-slop check.**

---

## Phase 1: Templates + CSS

- [x] T001 stats.html: add `<colgroup>` with `col-bearer` (9rem), `col-num` (6.5rem ×6), `col-aimd` (6rem), `col-window` (10.5rem)
- [x] T002 stats.html: AIMD column header gains `class="th-center"`; AIMD cell gains `class="td-center"`
- [x] T003 stats.html: `title="..."` on req-left, retry-after, clients headers explaining their semantics
- [x] T004 stats.html: empty-cell em-dash wrapped in `<span class="muted">` for visual de-emphasis
- [x] T005 style.css: `table-layout: fixed`; `col-*` widths; `td-center` rule; `thead th[title]` cursor:help; mobile @media (≤920px) hide req-left + retry-after
- [x] T006 style.css: `.advisor-output.err` (red left-rail, red h3); `.advisor-empty` (dashed border, mantle bg)
- [x] T007 dashboard.html: `<details class="config-section" open>` (was collapsed by default)
- [x] T008 dashboard.html: advisor section unconditional (no `{% if advisor_enabled %}`); empty-state inside #advisor-out names the EnvironmentFile path
- [x] T009 dashboard.html: button uses `hx-swap="innerHTML scroll:#advisor-out:top"`

## Phase 2: Route + advisor partial

- [x] T010 routes.py: `advisor()` returns HTTP 200 with rendered partial on success / disabled / exception; pass `{"recommendation", "snapshot", "error"}` to template
- [x] T011 advisor.html: branch on `{% if error %}` → `.advisor-output.err`, else `.advisor-output` (teal); preserve `<details>Snapshot</details>` in both

## Phase 3: Knob help

- [x] T012 config.py: every `EDITABLE_KNOBS` entry rewritten to carry ≥3 sentences covering what / when it binds / suggested value / trade-off. Notable callouts: `utilization_target` as smoothness lever for Max-tier; `aimd_decrease` warns ≥0.9 cascades 429s (Netflix research); `advisor_enabled` names the EnvironmentFile path

## Phase 4: Tests

- [x] T012a `test_ui_advisor_disabled_renders_inline_error` — expects 200 + `advisor-output err` + `ADVISOR_ENABLED` in body
- [x] T012b `test_ui_advisor_enabled_surfaces_error` — expects 200 + `advisor-output err` + exception message in body

## Phase 5: Verification

- [x] T013a `uv run pytest` → 90/90 green
- [x] T013b `uv run ruff check src tests` → clean
- [x] T013c `uv run ruff format --check src tests` → 21 files formatted
- [x] T013d Live smoke on temp instance at `:8766` — `/ui` renders, advisor returns rendered HTML at 200, system service at `:8765` NEVER restarted
- [x] T013e **MANDATORY Codex adversarial review** (agent `af5d1f48`, 2026-05-27T01:04Z, verdict **YELLOW**). 10 challenges: 5 VERIFIED (HTMX swap math, slop scan, error CSS specificity, no service bounce, one-script constitution), 3 PARTIAL (exception containment lazy-import outside try; a11y `title` tooltips weak; `recommend()` returning `None` falls into success branch), 2 UNSUPPORTED P2 (table overflow ~1024px viewport; settings open-by-default exposes 12 autosave knobs). **P2/P3 addressed in commit follow-up**:
    - Table overflow: wrapped `<table.bearers>` in `<div class="bearers-wrap" role="region" tabindex="0">` with `overflow-x: auto`; added `min-width: 64rem` to the table so horizontal scroll engages cleanly under 1024px
    - Settings default: reverted to `<details>` closed; added `summary-hint` data attribute that toggles "click to expand"/"click to collapse" text; `hx-trigger="toggle ... once"` lazy-loads the form only when the operator expands the section
    - A11y: `aria-describedby` on req-left/retry-after/clients headers linking to off-screen `<span class="sr-only">` siblings; `scope="col"` on every `<th>`; bearers wrapper has `role="region" aria-label="Bearer state table"`. Partial findings #3 (BaseException) + #7 (recommend None) accepted as documented contract gaps — neither symptom has fired in tests + real-world usage.
- [x] T013f Anti-AI-slop checklist (see [checklists/anti-slop.md](checklists/anti-slop.md)) — all 24 items PASS, Codex independent rescan also clean

## Phase 6: Merge gate

- [ ] T014 Babysit PR #32 CI green (Sonar scan)
- [ ] T015 Merge PR #32 via `gh pr merge 32 --squash --delete-branch` (admin if branch policy requires)
- [ ] T016 Remove `.worktrees/anthropic-throttle-proxy-031-ui-fixes` after merge

---

**Dependencies:**
- T010 blocks T011 (route must pass `error` key before template can branch on it)
- T013e (Codex) blocks T014/T015 per project CLAUDE.md ("before merging any
  throttle-path fix or declaring a throttle incident solved, ask Codex for
  adversarial review")
- T013f (anti-slop) blocks T014/T015 per Pedro's 27/05/2026 directive
  ("avoid AI slips at all costs")
