# Tasks — 281 team-seat telemetry

- [x] T0 spec/plan/tasks with hypothesis + falsifier (spec.md).
- [ ] T1 Probe: `team_seat_lane` (fail-closed matrix, counters-only) +
      `team_report` + `main()` team capture via `MIMO_TEAM_PROJECT_ID`.
      Accept: `pytest tests/test_mimo_plan.py` green.
- [ ] T2 lanes.py: `mimo:team-owner` id, distinct identity "MiMo Team",
      view() split filter (plan exactly-1 unchanged; team 0/1/>1), local
      snapshot drops both mimo ids. Accept: `pytest tests/test_lanes.py` green.
- [ ] T3 Full checks: `ruff check .` + targeted pytest + full pytest.
- [ ] T4 Commit owned files only (normal hooks); `result.md` with SHA,
      commands/results, integration contract.
