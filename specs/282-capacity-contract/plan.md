# Plan 282 — reuse the existing limiter; add one bounded check

## Approach (smallest missing piece)

The mechanics exist (`FairBearerLimiter` per-client RR + leases + bounded wait + AIMD
floor; `budget_paced`/`least_loaded` gates; plan meters; static pools). The plan adds
NO scheduler and touches NO production file:

1. `spec.md` — the bounded acceptance contract (definitions, C1–C6 accounting rules,
   F1–F6 fairness invariants, T1–T4 throughput rules).
2. `scripts/check-seat-fairness.py` — one executable, deterministic synthetic check
   that **drives the real `FairBearerLimiter`** from `src/` (import, not reimplement)
   and asserts the invariants that live in this repo's code, plus the contract's
   accounting guards as small pure functions in the script itself.

## Why driving the real limiter matters

F1/F2/F4/F5/F6 are properties of `limiter.py` (`_next_waiter`, `_cancel_cleanup`,
`_FairSlotContext`, `shrink`). Reimplementing them in the check would test the check,
not the code. The script imports `anthropic_throttle_proxy.limiter` and exercises the
public API only: `acquire_lease` / `release` / `slot(max_wait=…)` / `shrink`.
Config is pinned via env **before** import so the live cap is deterministic
(`THROTTLE_AIMD_INITIAL_CONCURRENT`, `THROTTLE_AIMD_MIN`, `THROTTLE_PRIORITY_RESERVE_SLOTS=0`).

## Determinism strategy

- Dispatch **order and counts** are asserted exactly: one event loop, controlled
  enqueue order (tasks created in a fixed sequence, each `acquire_lease` enqueues
  synchronously before its first await).
- Wall-clock **timings are measured and printed** (queue-wait p50/max, effective
  overlap, busy time) as informational outputs — never asserted beyond
  `overlap ≤ cap` and `overlap == cap` for the saturated phase, which hold for any
  scheduler that interleaves (both acquires happen before any sleep expires).

## Scenario matrix (simulation only)

| Phase | Question | Asserts |
|---|---|---|
| 1 RR fairness | does a chatty client starve a sibling backlog? | F1, F2 (exact interleaving) |
| 2 equal-eligible capacity | equal raw requests ≠ normalized utilization; asymmetric svc/caps; unavailable seat | F3, C5, T1 measurements (overlap, busy time) |
| 3 cancellation | does a cancelled waiter leak a slot/lease? | F4 (exact accounting) |
| 4 bounded wait | does `QueueWaitTimeout` leave a clean state? | F5 |
| 5 AIMD floor | can shrink empty the lane? | F6 |
| 6 accounting guards | mixed windows / ineligible seats / unassigned quota | C1, C2, C3, C5 |

## Deliverables and boundaries

- Files committed: `specs/282-capacity-contract/{spec,plan,tasks,result}.md`,
  `scripts/check-seat-fairness.py` (+ the coordinator's `assignment.md` for provenance).
- Commit locally with the repo's hooks (lefthook pre-commit). **No push/merge** —
  coordinator integrates to branch `279`.
- Lint: `uv run ruff check` on the new script; the check itself must exit 0.
