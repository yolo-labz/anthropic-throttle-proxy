# Spec — 281 team-seat MiMo telemetry (workspace w1P)

## Mission
Surface the MiMo **team seat** quota as its own truthful lane row beside the
existing individual plan row. Public code, synthetic fixtures only;
authentication/secrets/deployment belong to the coordinator.

## Hypothesis
The team seat's counters can be surfaced truthfully from the same supervised
console session that reads the individual plan — by allowlisting the seat
counters, deriving every percentage from them, and failing CLOSED (fresh,
explicitly non-usable row, never fabricated meters) on every non-usable state
(invalid / ambiguous / unassigned / expired / exhausted / stale).

## Falsifier
Any of these falsifies the hypothesis as implemented:
- an invalid, ambiguous, unassigned, expired or exhausted team reading renders
  `status=ok`, or carries meters whose numbers were not derived from
  `creditsTotal`/`creditsUsed`;
- a team failure leaves the previously written healthy team meter in place
  (no fresh fail-closed row is written);
- `projectId` / user id / `seatId`, `usedPercent`/`historyUsedPercent` display
  values, or any raw key material appears in the output;
- the individual `mimo:plan` row's allowance, schema or `report(detail, usage,
  now)` call shape changes;
- a missing Team configuration changes legacy individual-only behavior.

## Contract (from assignment.md)
- Report schema stays 1: `generatedAt`, `intervalSeconds`, `lanes[]`.
- `report(detail, usage, now)` and `mimo:plan` stay compatible; the new row is
  `mimo:team-owner` with `kind=mimo`. The team row never replaces the
  individual allowance and never adds to it (two independent rows).
- Seat response shape (synthetic, see assignment.md) via read-only route
  `/api/v1/project/<project-id>/teamTokenPlan/my/seat`; project id from an
  explicit environment identifier (`MIMO_TEAM_PROJECT_ID`), no real ids or
  credentials in source. Raw API keys are never fetched.
- Expected-account identity verification is maintained (`MIMO_EXPECTED_ACCOUNT_ID`).
- Use actual counters (`creditsTotal`, `creditsUsed`), not the redundant
  display percentages; reject bool/NaN/negative/ambiguous values.
- Project/user/seat ids must not leave the allowlisted output.
- `lanes.snapshot` filter (`mimo:plan` only) extends narrowly to
  `mimo:team-owner`, with distinct display identity ("MiMo Team").
- Account authorization is NOT proven by a quota row. Unassigned purchased
  seats are not usable capacity and their counters are never fabricated.

## Ownership
`scripts/mimo-token-plan-probe.py`, `src/anthropic_throttle_proxy/lanes.py`,
`tests/test_mimo_plan.py`, `tests/test_lanes.py`, this directory. Nothing else.
