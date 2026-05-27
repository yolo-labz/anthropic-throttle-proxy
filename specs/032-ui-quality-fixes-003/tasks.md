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
- [ ] T013e **MANDATORY Codex adversarial review** via `codex:codex-rescue` agent — challenge HTMX swap behaviour, advisor error path, table column math, anti-AI-slop fingerprint, accessibility regressions
- [ ] T013f Anti-AI-slop checklist (see [checklists/anti-slop.md](checklists/anti-slop.md)) — all 16 items PASS

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
