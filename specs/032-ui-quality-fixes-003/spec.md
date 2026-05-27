# Feature 032: UI Quality Fixes (Bearer Table, Advisor Surface, Knob Help, Anti-Slop)

**Spec date**: 2026-05-27
**Branch**: `031-ui-fixes` (PR #32)
**Worktree**: `.worktrees/anthropic-throttle-proxy-031-ui-fixes`

---

## Problem statement

Pedro reviewed the live `/ui` dashboard on 2026-05-27 and reported four defects
via screenshot:

1. **Bearer table misaligned.** Columns drifted apart; header text did not sit
   above its values. Padding inconsistent between cells.
2. **GROQ advisor "does not work".** Clicking *Ask advisor* produced no
   visible response.
3. **Lack of explanations on the knobs.** The 12 editable config knobs each
   carried one short sentence of help — insufficient to choose a value
   without external research.
4. **"and so on"** — broader anti-AI-slop hygiene gaps (no tooltip
   discoverability, no settings open-by-default, no error styling).

The proxy hot path is correct; this is a UI-only quality regression.

## Constraints

C1 The running system service at `127.0.0.1:8765` MUST NOT be bounced
   during the fix rollout. Pedro's claude code session must survive.

C2 No proxy hot-path behaviour change. Scope is templates, CSS, the
   `/ui/advisor` route's error rendering, and `EDITABLE_KNOBS` help text.

C3 HTMX 1.x dashboard rule (constitution VI) — one `<script>` tag, no
   Alpine/React/ESM modules, server-rendered HTML.

C4 Catppuccin Mocha tokens only — raw hex permitted only inside
   `style.css :root`. One radius scale + one shadow scale per surface class.

C5 Anti-AI-slop ban list applies (`~/.claude/rules/anti-ai-slop.md`): no
   gradient backgrounds, no emoji icons, no cookie-cutter cards with
   uniform shadows, no Lorem ipsum, no Inter-only typography, no skipped
   heading hierarchy, no bounce easing, no glassmorphism, no symmetric
   3-column AI-starter-pack grids.

## User stories

**US1** (operator at 4am) — *I open `/ui` and need every bearer column to
sit above its values without horizontal hunting.* Acceptance: header
text aligns with its cell content for all 9 columns; tooltip on every
column that needs context.

**US2** (operator debugging a throttle event) — *I click Ask advisor and
need a clear in-page response, success or failure.* Acceptance: button
click ALWAYS renders a visible response inside `#advisor-out` within
5 seconds, with error styling if disabled / failed.

**US3** (operator tuning) — *I open settings and need each knob's help
text to tell me what / when it binds / suggested value / trade-off
without leaving the page.* Acceptance: every entry in
`EDITABLE_KNOBS` carries ≥3 sentences covering those four points.

**US4** (reviewer auditing aesthetics) — *I scan `/ui` and need zero
anti-AI-slop fingerprints.* Acceptance: checklist of 16 banned
patterns from `anti-ai-slop.md` all marked PASS.

## Functional requirements

- **FR-001** Bearer table MUST use `table-layout: fixed` with `<colgroup>`
- **FR-002** AIMD column MUST be centre-aligned
- **FR-003** API-key-only columns MUST carry a `title=` tooltip
- **FR-004** Mobile (<920px) MUST hide req-left + retry-after columns
- **FR-005** `POST /ui/advisor` MUST return HTTP 200 always (HTMX 1.x
  silently drops non-2xx swaps)
- **FR-006** Advisor partial MUST render with `.advisor-output.err`
  (red left-rail) on error vs. teal on success
- **FR-007** Settings `<details>` MUST default to `open`
- **FR-008** Every `EDITABLE_KNOBS` entry MUST carry ≥3 sentences of
  help covering what / when / suggested / trade-off
- **FR-009** Advisor button MUST use `hx-swap="innerHTML scroll:
  #advisor-out:top"`
- **FR-010** Advisor disabled empty-state MUST name the EnvironmentFile
  path so operator knows where to set `GROQ_API_KEY`

## Success criteria

- **SC-001** Visual smoke shows all 9 bearer columns aligned, AIMD centred
- **SC-002** `POST /ui/advisor` disabled → HTTP 200 + `advisor-output err`
  + `ADVISOR_ENABLED` in body
- **SC-003** `POST /ui/advisor` with `recommend` raising → HTTP 200 +
  `advisor-output err` + exception message in body
- **SC-004** `pytest` 90/90 green; `ruff check` clean; `ruff format --check`
- **SC-005** Anti-AI-slop 16-item checklist all PASS
- **SC-006** System service `:8765` has same MainPID before/after — no bounce
- **SC-007** Codex adversarial review GREEN or YELLOW with findings closed

## Out of scope

- Proxy hot-path / forwarding / AIMD / unified-window code paths
- Backend persistence changes
- New JS frameworks (Alpine, React, ESM modules)
- Auth on the `/ui` surface

## Assumptions

A1 The dashboard is loopback-only (127.0.0.1:8765); no auth needed.
A2 Operators run modern evergreen browsers (Chrome/Firefox/Safari).
A3 Pedro's Max-tier OAuth bearers don't populate API-key headers, so
   req-left / retry-after columns are routinely empty.
A4 The runtime overrides file at `~/.local/state/anthropic-throttle-proxy/
   overrides.json` survives restart, so visual changes via `/ui/config`
   persist independently of HM activation.
