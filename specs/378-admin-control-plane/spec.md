# 378 — Admin control plane (phase 1: read-only consumer view)

Source: `Throttler/loop/briefs/admin-control-plane.md`, derived from W2 (`Throttler/research/2026-10-31-week-coverage-breakthroughs/W2-admin-control-plane.md`).

## Problem
The weekly Claude quota is shared by every fleet consumer, but nothing shows who burns what against an allotment. W2 measured: no workspace/role identity reaches the gateway (client ids are TCP peer ports), and the `x-anthropic-throttle-project` header is never sent by pi.

## Data model (target, phases 2+)
consumer = project/session/workspace/role → tier, weekly share, cap, protect, pause. Phase 1 reads only what `project_share.ProjectShare` already holds: per-project spend in the 5 h window and weight `w_p`.

## Identity
`x-anthropic-throttle-project` (sanitized by `project_share.sanitize`). Absent or unusable → `_direct`. Identity-stamp slice (pi seats send the header) is a separate slice.

## Phase 1 scope (this spec)
- `GET /__throttle/consumers` — JSON snapshot.
- `GET /ui/consumers` — read-only HTML table of the same snapshot.
- Rows: project, `usd_5h`, `share_5h` (used/capacity), `entitlement` (g_p = U·w_p/Σw_active), weight, over-share flag, active flag. `_direct` row always present.
- Allotment source: the **existing** entitlement `g_p`, not `THROTTLE_CLIENT_SHARES`. That knob does not exist in the codebase (grep: 0 hits), so it is not used.
- Default off: routes are registered only when `THROTTLE_CONSUMERS_VIEW=1`.
- Latency: <50 ms with 500 consumers (invariant 4 spirit; CPU-only, no I/O).

## Explicitly NOT in phase 1
- No write endpoints (no set_tier, set_share, pause/resume, MCP tools). Existing `/ui/projects/weight` is untouched.
- No dry-run, audit log, or admin page `/ui/admin`.
- No changes to routing, limiter, or pacing behaviour. Read-only.

## Acceptance
- Endpoint and snapshot <50 ms with 500 synthetic consumers.
- Absent / unusable project header → `_direct` row.
- No POST/PUT/DELETE routes under `/__throttle/consumers` or `/ui/consumers`; routes absent when the env flag is unset.
- `ruff check` and `ruff format --check` clean on touched files.
