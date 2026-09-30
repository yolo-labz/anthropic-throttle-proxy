# result — 281 team-seat MiMo telemetry (workspace w1P)

30/09/2026 16:07–17:2x BRT · branch `281-team-meter` · base `2e7a43f`

## Commit

**`378b8c3`** — `feat(lanes): truthful per-seat MiMo team telemetry (spec 281)`
(files: `scripts/mimo-token-plan-probe.py`, `src/anthropic_throttle_proxy/lanes.py`,
`tests/test_mimo_plan.py`, `tests/test_lanes.py`, `specs/281-team-meter/`.)
This report is a second commit on the same branch. Nothing pushed; no deploy,
no UI/routes/routing/proxy/config edits, no account access.

## Hypothesis and falsifier (as implemented)

**Hypothesis:** the team seat's counters can be surfaced truthfully from the same
supervised console session as the individual plan — allowlist the seat counters,
derive every number from them, and fail CLOSED (fresh explicit unusable row,
never fabricated meters) on every non-usable state.

**Falsifier (each item tested):** an invalid/ambiguous/unassigned/expired/
exhausted reading rendering `ok` or carrying invented meters; a team failure
leaving the previous healthy team meter in place; project/user/seat ids or the
payload's display percentages reaching the output; any change to `mimo:plan` or
`report(detail, usage, now)`; missing Team configuration changing legacy
individual-only behaviour.

## Executed commands / results

```
$ uv run ruff check .
All checks passed!

$ uv run pytest tests/test_mimo_plan.py tests/test_lanes.py -q
77 passed in 0.32s

$ uv run pytest -q
1434 passed, 122 warnings in 88.45s
```

One test-side defect found and fixed during the run (an assertion hard-coded a
fixture value the parametrization overrode); no implementation change was
needed to reach green.

## What the code does

- `team_seat_lane(seat_response, now)` builds the `mimo:team-owner` row
  (`kind=mimo`) from the read-only route shape
  `/api/v1/project/<project-id>/teamTokenPlan/my/seat`:
  - counters-only: `usedPercent` is derived as `creditsUsed/creditsTotal`;
    the payload's `usedPercent`/`historyUsedPercent` are ignored (tests feed
    lying display values and assert the derived ones);
  - bool/NaN/negative/non-numeric counters, `creditsTotal <= 0`,
    `historyCreditsUsed < creditsUsed` (ambiguous), unparseable
    `currentPeriodEnd`/`nextResetTime` → fail closed (`status=unknown`, no
    meters), reason names the class;
  - `seatStatus != "ASSIGNED"` (unassigned/pending/revoked/unknown) → fail
    closed with no meters: an unassigned purchased seat is not usable capacity
    and its counters are never fabricated;
  - `expired: true`, period ended, or `used >= total` → `status=exhausted`
    with only the measured counters (honest numbers, closed verdict);
  - project/user/seat ids never enter the row; no key material is fetched.
- **Fail closed is a fresh ROW, never an exception:** every failure class
  RETURNS a row. Raising would skip the report write and silently retain the
  previously written healthy team meter until staleness — the exact anti-goal
  (tested by `test_team_failure_writes_a_fresh_fail_closed_row_instead_of_raising`
  and the view-level replacement test).
- `team_report(detail, usage, seat, now)` = the unchanged `report(...)` plus the
  team row; `report(detail, usage, now)` and `mimo:plan` are untouched
  (`test_legacy_report_shape_is_unchanged_without_team`).
- `main()` captures the team route only when `MIMO_TEAM_PROJECT_ID` is set (an
  explicit environment project identifier; no real ids in source); a missing
  team response still writes the fail-closed row. Unset = exact legacy
  behaviour.
- `lanes.snapshot` (`view()`): the `mimo:plan` filter/`exactly-1` rule is
  unchanged; team rows are accepted narrowly (0 rows → no team row, legacy
  individual-only preserved; >1 → synthetic `unknown`, fail closed). The local
  snapshot drops BOTH mimo ids before report rows are appended, so a stale copy
  of either row can never outlive the report that dropped it
  (`test_a_local_team_row_cannot_outlive_the_report_that_dropped_it`).
- Distinct display identity: `mimo:team-owner` renders **"MiMo Team"**;
  `mimo:plan` keeps **"MiMo"** (only this suffix is special-cased).

## Remaining integration contract (coordinator owns this)

1. Set `MIMO_TEAM_PROJECT_ID` on the browser host for the seat to be read;
   without it the probe is byte-for-byte the legacy individual-only probe.
2. `MIMO_EXPECTED_ACCOUNT_ID` verification is unchanged — the team reading is
   behind the same identity check.
3. Report schema stays 1 (`generatedAt`, `intervalSeconds`, `lanes[]`); the new
   row is `mimo:team-owner` with one meter labelled `seat` (`windowMins: None`
   — no burn-pace invention). `plan_meter_used_percent("mimo:team-owner", …)`
   works unchanged via its lane-id argument.
4. **A quota row is NOT proof of account authorization** — do not wire the team
   row into any admission/authorization decision; it is telemetry only.
5. Unassigned seats are not capacity. If the dashboard should distinguish
   "unassigned" from "reading invalid" as separate statuses, that is a
   rendering decision (current vocabulary: `ok`/`exhausted`/`unknown` +
   reason text).
6. Cherry-pick `378b8c3` (+ this file's commit) into 279; the four owned files
   and `specs/281-team-meter/` are the whole blast radius.
