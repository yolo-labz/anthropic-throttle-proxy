# 290 — exhaustion classification + stale/unknown capacity fail-closed

## Receipts (real times, BRT)

| Time | Event | Result |
|---|---|---|
| 02/10 23:44:03 | Sampler service refresh triggered after storage recovery (pJ/Ops p6 restored 36.9 GB free) | probe exit 0 |
| 02/10 23:44:09 | Fresh `mimo-plan.json` written: `generatedAt=2026-10-03T02:44:09Z` | cadence unblocked |
| 02/10 23:44:09 | Fresh sample classified `mimo:plan` **`ok` at 100.044%** — used ≥ total with a future reset | FAIL reproduced on fresh data (the defect) |
| 02/10 23:5x–00:0x | Fix + synthetic regression authored on base `ebe8926` (#283) | see below |

## What is fixed here (public code)

1. **`used >= total` exhaustion classification (probe source).**
   `scripts/mimo-token-plan-probe.py::report()` now classifies a fully-consumed
   meter as `exhausted` regardless of reset time, matching the team seat rule
   (`used >= total`) already in `team_seat_lane()`. The raw report is now
   truthful so no consumer has to re-derive it.
2. **Stale/unknown capacity fail-closed — verified and PINNED (no behavior
   change needed).** Inspection of `lanes._normalize`/`_capacity_verdict` and
   `ui/presentation.row_capacity_class` shows the fail-closed path is already
   implemented (spec 285 capacity truth):
   - stale → `stale` (never usable), unknown → `unknown` (never usable);
   - `ok` needs positive measured evidence; a full meter downgrades `ok` →
     `exhausted` even if the probe said `ok` (defense in depth — this is what
     made the UI render `mimo:plan` exhausted while the raw report said `ok`).
   New synthetic regressions pin all six class outcomes so the contract cannot
   silently regress.

## Synthetic regression evidence

- `tests/test_mimo_plan.py` + `tests/test_capacity_view.py`: **78 passed**
  (3 new probe-level assertions + 6 parametrized capacity-class cases).
- Targeted scope only (no full-suite storm, no live provider calls, no Z.AI
  replay — admission stays closed).

## Out of scope (original owners, preserved)

- pH: per-assigned-seat meter row for seat B (serving bearer `07c7ae8a`) —
  catalogue gap recorded in `specs/279-team-capacity/xiaomi-catalogue-receipts.md`.
- pK: routing exclusion end-to-end after truthful classification.
- pJ/Ops p6: CI timer stays paused until root activates verified PR2598 leaf;
  this PR publishes clean and waits for that CI window.
- pF: private account B identity / raw quota proof stays protected; no
  credentials appear in this packet. No new purchase, bearer or concurrency.
