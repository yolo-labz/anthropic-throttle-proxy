# Plan — 281 team-seat telemetry

Smallest correct diff, all inside owned files.

## Probe (`scripts/mimo-token-plan-probe.py`)
- `report(detail, usage, now)` untouched (compat).
- New `team_seat_lane(seat, now) -> dict` builds the `mimo:team-owner` row.
  Design decision: it NEVER raises for payload problems. A raise skips the
  report write, which silently retains the previous healthy team meter until
  staleness — the exact anti-goal. Instead every failure class returns a fresh
  FAIL-CLOSED row:
  - `code != 0` / missing response / unparseable payload → `status=unknown`,
    no meters, reason names the class;
  - `seatStatus != "ASSIGNED"` (unassigned/pending/revoked/unknown) →
    `status=unknown`, no meters ("seat unassigned — not usable capacity");
  - `expired: true` or `currentPeriodEnd <= now` → `status=exhausted` WITH the
    real (validated) counters — measured numbers, like the plan row does;
  - counters bool/NaN/negative/non-numeric, `creditsTotal <= 0`,
    `historyCreditsUsed < creditsUsed` (ambiguous) or unparseable dates →
    `status=unknown`, no meters.
  Meters derive `usedPercent` from `creditsUsed/creditsTotal`; the payload's
  `usedPercent`/`historyUsedPercent` are display values and are ignored.
  `projectId`/user/`seatId` never enter the row.
- New `team_report(detail, usage, seat, now) -> dict` = `report(...)` plus the
  team row (fail-closed row when `seat` is None/unavailable). `main()` uses it
  when `MIMO_TEAM_PROJECT_ID` is set; otherwise legacy `report(...)` exactly.

## lanes.py
- `_MIMO_TEAM_LANE_ID = "mimo:team-owner"` beside the plan id.
- `_lane_identity`: suffix `team-owner` under kind `mimo` → `"MiMo Team"`
  (plan keeps `"MiMo"`). Narrow: no other identity changes.
- `view()` mimo filter: split plan rows (exactly-1 rule unchanged, synthetic
  `unknown` fallback) from team rows (0 rows → no team row, legacy
  individual-only preserved; 1 → use; >1 → synthetic `unknown` team row). The
  local snapshot drops BOTH mimo ids before report rows are appended, so a
  stale copy (of either row) can never be silently retained.

## Tests (exact paths)
- `tests/test_mimo_plan.py`: team row success (counters-derived, no ids, no
  display percents, independent allowance), fail-closed matrix (invalid,
  ambiguous, unassigned, expired, exhausted, missing), "failure writes a fresh
  fail-closed row" (the retention falsifier), legacy `report()` compat.
- `tests/test_lanes.py`: snapshot accepts the team row with distinct identity;
  ambiguous team rows → synthetic unknown; absent team row → legacy shape;
  stale report degrades the team row.

## Acceptance
`pytest tests/test_mimo_plan.py tests/test_lanes.py` green, `ruff check .`
clean, full `pytest` green if feasible. Commit owned files only.
