# Tasks — spec 377 phase 1

- [x] T001 Pure allocator rules + parsers (`usage_allocator.py`).
- [x] T002 Config knobs, fail-closed defaults (`config.py`).
- [x] T003 Spill-only routing filter (`proxy.py`, enforce only).
- [x] T004 Pre-claim defer with catch-up Retry-After; priority passes (`proxy.py`).
- [x] T005 Read-only `weekly_pacing` view in `/__throttle/admission`.
- [x] T006 Acceptance tests A1–A4 + parsing/finite-key tests (25 passing across 2 files).
- [x] T007 Mutants: `burst := inf` (5 fail), `Retry-After := 5` (2 fail).
- [ ] T008 Full suite + PR CI (phase 1 gate).
- [ ] T009 Observe-mode data collection (phase 2 input). Not in this PR.
- [ ] T010 Client-share enforcement (phase 2). See phase 2 below.

# Tasks — spec 377 phase 2 (allocator-phase2, 31/10/2026)

- [ ] T011 Attribution: `weekly_project()` = `x-anthropic-throttle-project`, else `X-Throttle-Client-Id` label, else `_direct`. Header stays stripped upstream (`_forward_headers`).
- [ ] T012 Measured tokens per project over a trailing 7 d window (`TokenLedger`, hourly buckets, bounded). Zero/absent usage is never counted (unknown ≠ free).
- [ ] T013 Share enforcement under `THROTTLE_WEEKLY_PACING=enforce`: defer over-share non-priority calls ONLY when the fleet is ahead (`fleet_pace > 1`) AND another configured client is active (borrowing when idle). Priority exempt. Honest catch-up Retry-After via the stamped 503.
- [ ] T014 Multi-window burn alert: projected end-of-7d ratio (`burn_projection`) over 24 h AND 6 h; fires only when both exceed 1.0. Metric `anthropic_weekly_burn_projection{account,window}`, counter `anthropic_weekly_burn_alerts_total{account}`, one log line per transition. Alert only, never refuses.
- [ ] T015 `/__throttle/admission` gains `weekly_tokens` and `burn` (read-only, O(accounts)); `client_shares.enforced` reflects the mode.
- [ ] T016 Tests: header strip + absent=`_direct`; share defer only when fleet ahead; served when fleet under schedule; priority exempt; burn alert on sustained 1.20× replay, not on a 5 h spike. Mutants: drop fleet-ahead → fail; single-window alert → spike test fails.
- [ ] T017 Gates: ruff, targeted tests, PR CI green, squash-merge, post-merge main green.
