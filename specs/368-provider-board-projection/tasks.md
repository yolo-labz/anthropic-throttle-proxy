# Tasks

- [x] Inspect current YAML/source/runtime boundary.
- [x] Red regression: six failures on clean origin/main (validation + ghost providers).
- [x] Implement explicit presentation-only destination filtering.
- [x] Pass targeted tests and offline full-page/repeated-poll rendering (108 passed).
- [x] Pass isolated full pytest and ruff; preserve command receipts (2400 passed; lint/format exit 0).
- [x] Validate deployed config, durable rollback/hash and race-check before live YAML replacement (parser exit 0; same running PID; destination filtering not activated).
- [ ] Selective PR delivery; verify CI/merge or precise bounded blocker.
- [ ] Durable owner handoff with live/source distinction and reversal.
