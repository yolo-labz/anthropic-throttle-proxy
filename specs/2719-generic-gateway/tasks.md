# Tasks: 2719 Throttler source migration

- [x] T001 Inspect existing generic routing/config, distribution, callers and UI.
- [x] T002 Record Speckit spec/plan/research/data/entrypoint contract/acceptance.
- [x] T003 [P1] Add failing canonical/legacy installed-entrypoint acceptance.
- [x] T004 [P1] Publish canonical distribution/package and both commands.
- [x] T005 [P2] Update UI/brand/current docs and operational samples.
- [x] T006 Verify focused/full tests, wheel/sdist and lint within small thread1.
- [x] T007 Update handoff/unique status with exact source and exports.
- [ ] T008 Normal hooks, PR, exact-head required CI and zero unresolved threads.
- [ ] T009 Verify safe squash merge; export source SHA/CI/revert path to root.

Dependency order: T001 → T002 → T003 → T004 → T005 → T006 → T007 → T008 → T009.
This bounded source slice is executed in one worktree without fleet delegation.

Delivery tasks T008/T009 are tracked by the external exact-head status receipt
linked in [evidence.md](evidence.md); it records final CI and merge without a
self-referential commit hash in this source snapshot.
