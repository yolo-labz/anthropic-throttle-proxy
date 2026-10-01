# Spec 285 — consolidated, truthful quota/capacity UI (workspace w1P)

## Problem

The web UI shows subscription meters and live routing, but the two surfaces
disagree and the disagreements read as health. Reproduced shape (synthetic
data only — no screenshots, console or account records):

1. A provider's subscription meter reads **EXHAUSTED** while its Live routing
   row reads **HEALTHY**, because the routing row's verdict is transport/DNS
   reachability. Service connectivity is being read as usable quota.
2. A separately assigned **Team** subscription is **absent** from the board
   because only the individual plan meter is shown (singleton row
   assumptions), so the fleet's real capacity understates itself.

Beyond the reproduction, the board has no at-a-glance answer to "how many
seats can actually serve traffic right now, what is binding, and what is live
load" — and per-seat rows never show *remaining*, only used %.

## Scope fences (from `assignment.md`)

- Workspace: this worktree only, branch `285-capacity-dashboard`, base `2e7a43f`.
- Own: `src/anthropic_throttle_proxy/ui/`, `fleet_ui_config.py`, UI tests,
  `specs/285-capacity-dashboard/`.
- Do NOT edit `lanes.py`, probe, proxy/account/routing/config production code.
- The `229-dashboard-truth` worktree is unrelated WIP — untouched.
- No private account/Notes access, no deployment, push, merge, extra workers,
  or work outside workspace w1P. Commit this slice only; the coordinator
  cherry-picks into 279 and verifies the live runtime.

## Requirements

- **FR-1 — At-a-glance capacity summary.** The board carries a summary of
  seats by capacity class: **usable / exhausted / stale / unknown** (plus
  **limited** for partially-constrained multi-pool seats). Classification is
  evidence-based: only measured capacity (headroom %, an unlimited product, a
  positive balance/remaining reading) counts as usable; a configured row with
  no reading (an unassigned seat) is unknown, never usable; stale and unknown
  are never healthy; a measured refusal (rejected/exhausted/refused/locked/
  token expired) is exhausted.
- **FR-2 — Binding windows, never a fictitious total.** The summary lists the
  binding windows (row, window label, measured %, reopen countdown) of the
  seats whose evidence is current. Unlike percentages/units/windows are NEVER
  summed or averaged into one capacity number.
- **FR-3 — Per-seat used/remaining/reset.** Each meter renders its used %,
  its remaining/allowance when the provider reports them, and its reset
  (countdown + absolute stamp). Values render exactly as measured.
- **FR-4 — Live load and throughput.** The summary shows live
  inflight/queued/capacity and useful throughput (tokens/s) where measured;
  an unmeasured throughput is absent, never zero-claimed.
- **FR-5 — Transport ≠ capacity ≠ eligibility.** Live-routing rows distinguish
  three labelled facts: TRANSPORT availability (reachability, key rejection,
  DNS — DNS resolution alone never implies quota), SUBSCRIPTION CAPACITY
  (joined from the seat rows that belong to that provider), and MODEL
  ELIGIBILITY (unverified — this UI cannot verify it). A reachable row whose
  seats are exhausted must read `transport ok` + `capacity exhausted`, never
  `HEALTHY`.
- **FR-6 — No singleton lane assumptions.** Every normalized lane row renders,
  including `mimo:team-owner` alongside `mimo:plan` (schema 1 meter fields,
  distinct identity). Rows of one kind are counted, joined and rendered
  individually; nothing keys on a single hard-coded lane id.
- **FR-7 — Copilot products stay separate.** A spent premium meter with
  unlimited chat/completions never marks the seat (or its routing row)
  exhausted: only the spent meter is tagged, and the seat counts usable.
- **FR-8 — Rendering invariants preserved.** Progressive HTMX partial
  rendering, jinja autoescape for every caption/label/detail, zero new JS
  modules or dependencies, Catppuccin Mocha tokens only, existing font scale
  and layout rules intact.

## Non-goals

- No change to admission, credentials, routing, pacing, probe or meter
  schema; no `lanes.py` edit (the report passthrough for `mimo:team-owner`
  is the coordinator's integration).
- No live inference, browser runs against real accounts, or deployment.
- No claim about model eligibility, provider policy, or live runtime state.
