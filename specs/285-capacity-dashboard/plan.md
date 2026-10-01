# Plan 285 — consolidated, truthful quota/capacity UI

## Approach

Smallest test-backed diff inside the owned fence. All new logic is
display-only projection (never admission/credential policy), pure and unit
testable against synthetic rows.

### Touch points (all owned)

| File | Change |
|---|---|
| `ui/presentation.py` | pure helpers: `row_capacity_class`, `capacity_summary`, `attach_provider_capacity` (provider↔seat join by normalized id-prefix/provider tokens — no family join, so `openai` upstream never absorbs `codex:*` seats) |
| `ui/routes.py` | `_lane_meters` passes `remaining`/`allowance`/`balance_total` through; `_collect_view` (projected path) attaches provider capacity + `summary` |
| `ui/templates/partials/stats.html` | summary strip (counts, binding windows, live line), per-meter remaining, provider row triple: transport / capacity / models-unverified |
| `ui/static/style.css` | chip styles on existing tokens + font scale |
| `tests/ui_render.py`, `tests/render_preview.py` | context keys for the new surface |
| `tests/test_capacity_view.py` | synthetic acceptance for FR-1..FR-8 |

### Classification (FR-1) — evidence, not vibes

`row_capacity_class(row)`:

1. `stale` status → **stale** (a stale reading is not capacity).
2. measured refusal (`exhausted|rejected|refused|locked|token expired`) → **exhausted**.
3. `pool limited` → **limited** (a spent pool is not a dead lane).
4. `ok` → trust the meters: usable with measured headroom / an unlimited
   product / a positive balance or remaining; exhausted when every reading is
   full and nothing else is live; **unknown** when nothing was measured.
5. anything else (`unknown`, `unseen`, `error`, no reading) → **unknown**.

The provider capacity chip is worst-wins over the joined seats:
exhausted > stale > unknown > limited > usable, and `unmeasured` when the
join finds no seat row (honest gap, not health).

### Concurrency with other slices

- `mimo:team-owner` arrives via normalized lane rows (meter worker + coordinator
  `lanes.py` integration). This slice must render/count/join N rows of one
  kind with distinct identity — nothing keys on `mimo:plan`.
- No edits outside the fence; commit only this slice.

## Verification

1. Focused: `uv run pytest tests/test_capacity_view.py tests/test_ui_status.py
   tests/test_ui_truth.py tests/test_ui_layout.py tests/test_ui_display_config.py
   tests/test_ui_binding.py tests/test_ui_icons.py tests/test_ui_contrast.py
   tests/test_ui_focus_stability.py tests/test_dashboard_data_contract.py
   tests/test_dashboard_visibility_acceptance.py tests/test_dashboard_audit_acceptance.py`
2. `uv run ruff check src tests` + `uv run ruff format --check src tests`
3. Full `uv run pytest` when feasible.
4. `tests/render_preview.py` renders the new surface (dev eyeball tool).
