# Tasks 282 — capacity contract + synthetic fairness check

## Spec (contract)

- [x] T001 Inspect `budget_paced`, `least_loaded`, `FairBearerLimiter`, plan meters and
  static credential pools; name the smallest missing contract (spec §1).
- [x] T002 Define eligibility and eligibility-normalized utilization vs equal raw
  requests (spec §2).
- [x] T003 Write the hard accounting rules C1–C6 (spec §3), including no mixed-window
  sums, no unit mixing, unassigned ≠ active quota, no runway claim.
- [x] T004 Write the fairness invariants F1–F6 bound to existing code (spec §4).
- [x] T005 Write the throughput measurement rules T1–T4 with falsifiable tuning
  targets and halt conditions (spec §5).

## Check (executable)

- [x] T101 `scripts/check-seat-fairness.py`: drive the real `FairBearerLimiter` with
  pinned env; phase 1 client-RR interleaving (F1/F2).
- [x] T102 Phase 2: equal-eligible seats with asymmetric service/caps + one unavailable
  seat; measure completed work/time, effective overlap, queue wait, distribution (F3,
  C5, T1).
- [x] T103 Phase 3 cancellation accounting (F4) and phase 4 bounded-wait
  `QueueWaitTimeout` with no leak (F5).
- [x] T104 Phase 5 AIMD floor under repeated shrink (F6); phase 6 accounting guards
  (C1/C2/C3/C5).
- [x] T105 Run the check to exit 0; run repo lint on the new file; record both in
  `result.md` with the commit SHA.

## Boundaries (explicitly unchanged)

- [x] T201 No production code, tests, lanes, credential pools, deployment, pushes,
  browser/credentials/private vault, or additional workers touched.
