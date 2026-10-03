# B seat meter row — per-assigned-seat catalog gap

## Hypothesis and falsifier

The MiMo report carries a meter row for the individual plan (`mimo:plan`) and
the owner Team seat (`mimo:team-owner`) but not for the B seat — the second
Team seat and currently the only usable one — so dashboard capacity for that
allowance is invisible. A row per ASSIGNED seat, independent and fail-closed
like the existing two, closes it.

**Falsifiers (each tested):** the B row replacing, merging with or summing into
the owner/plan allowances; an unassigned or invalid B payload rendering `ok`
or fabricated meters; a failed B read silently retaining a previously healthy
B meter; the B row being skipped while configured; project/seat identifiers
reaching the output; any change to the legacy `mimo:plan` / `mimo:team-owner`
shape when the B seat is not configured.

## Scope

Report contract + consumer identity/merge pin only. **Synthetic fixtures
only**: no credentials, no account-B identity or raw quota evidence (pF's
front), no authenticated fetch, no new browser tabs. The live B-seat read
(account B's session) is a separate wiring step; `team_report`'s
`seat_b_response` is the seam it will drive.

- `scripts/mimo-token-plan-probe.py` — `mimo:team-seat-b` row via
  `team_seat_b_lane` (re-identifies the single validated seat-row builder, so
  owner and B cannot diverge); `team_report(..., seat_b_response=)` adds one
  fresh row per configured seat (fail-closed on failure, `_UNSET` keeps the
  spec-281 shape exactly).
- `src/anthropic_throttle_proxy/lanes.py` — identity `MiMo Team B` and the
  same zero/one/ambiguous merge pin the owner row has (spec 285).
- `tests/test_mimo_plan.py` — synthetic explicit counters per seat.

## Contract notes

- Rows are independent: nothing is ever summed across seats (spec 282
  dimension rules), and unassigned is never capacity.
- `used >= total` → `exhausted` with measured counters kept (same rule as
  #284 applied by the shared seat builder).
- Ids are fixed synthetic labels — never real project/user/seat identifiers.
