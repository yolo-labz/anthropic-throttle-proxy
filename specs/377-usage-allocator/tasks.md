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
- [ ] T010 Client-share enforcement (phase 2). Not in this PR.
