# Tasks 285 — consolidated, truthful quota/capacity UI

- [x] T000 Spec/plan/tasks in `specs/285-capacity-dashboard/` (this directory).
- [ ] T001 `presentation.py`: `row_capacity_class` + meter-evidence helper
      (FR-1/FR-7), pure and dict-tolerant.
- [ ] T002 `presentation.py`: `capacity_summary` (FR-1/FR-2/FR-4) — seat class
      counts, binding-window list, live line, throughput only when measured.
- [ ] T003 `presentation.py`: `attach_provider_capacity` (FR-5/FR-6) —
      normalized-token join per lane, worst-wins chip, `unmeasured` fallback.
- [ ] T004 `routes.py`: pass `remaining`/`allowance`/`balance_total` through
      `_lane_meters` (FR-3); wire capacity + summary in `_collect_view`.
- [ ] T005 `stats.html` + `style.css`: summary strip, per-seat
      used/remaining/reset, provider triple chips (FR-3..FR-8).
- [ ] T006 `tests/ui_render.py` + `tests/render_preview.py`: new context keys;
      preview shows the surface.
- [ ] T007 `tests/test_capacity_view.py`: synthetic reproduction + contract
      tests (exhausted meter vs reachable transport row; `mimo:team-owner`
      beside `mimo:plan`; copilot premium vs unlimited products; unknown/stale
      never usable; no fictitious totals; escaping; live line; `_collect_view`
      wiring).
- [ ] T008 Focused UI tests, ruff, full pytest; fix fallout in owned tests only.
- [ ] T009 Commit slice (normal hooks) + `result.md` with SHA, executed tests,
      limitations.
