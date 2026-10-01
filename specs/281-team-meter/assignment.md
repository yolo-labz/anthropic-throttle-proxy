# Team seat telemetry — workspace w1P

## Mission
Implement truthful per-seat MiMo quota telemetry alongside the existing individual plan. This is a public-code task with synthetic fixtures only. Coordinator owns authentication, secrets and deployment.

## Ownership
Work only in `/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-281-team-meter` (branch `281-team-meter`, base `2e7a43f`). Own `scripts/mimo-token-plan-probe.py`, `src/anthropic_throttle_proxy/lanes.py`, `tests/test_mimo_plan.py`, `tests/test_lanes.py`, and this spec directory. No UI/routes, routing/proxy/config edits; five existing workspace seats are parallel.

## Contract and evidence
Existing report schema is 1, with generatedAt, intervalSeconds, lanes[]. Keep `report(detail, usage, now)` and `mimo:plan` compatible. Add `mimo:team-owner` (kind=mimo), never replace or add the individual allowance to it. Generic authenticated response SHAPE (not real account data):
`{"code":0,"data":{"projectId":"project-example","planCode":"example","planName":"Example Team","currentPeriodEnd":"2030-02-01 23:59:59","settlementType":"example","autoRenew":true,"expired":false,"seat":{"seatId":"opaque","seatStatus":"ASSIGNED","creditsTotal":1000,"creditsUsed":120,"historyCreditsUsed":120,"usedPercent":12.0,"historyUsedPercent":12.0,"nextResetTime":null}}}`
Read-only route shape: `/api/v1/project/<project-id>/teamTokenPlan/my/seat`. Use an explicit environment project identifier if needed; no real IDs/credentials in source. Maintain expected-account identity verification. Never fetch raw API keys. Team failure must not silently retain a fresh healthy Team meter; invalid/stale/unassigned/expired/exhausted must fail closed. Missing Team configuration preserves legacy individual-only behavior. Use actual counters, not the redundant display percentage, and reject bool/NaN/negative/ambiguous values. Project/user/seat IDs must not leave the allowlisted output.

`lanes.snapshot` currently filters external MiMo reports to exactly `mimo:plan`; extend narrowly for the Team owner row and distinct display identity. Account authorization is NOT proven by a quota row. Unassigned purchased seats are not usable capacity; never fabricate their counters.

## Acceptance and delivery
Follow local constitution and speckit plan→tasks→implement; minimum spec/plan/tasks in this directory. State hypothesis/falsifier, write exact-path tests, run targeted pytest and ruff plus full pytest if feasible. No vendor SDK/dependency. No browser, rbw, private vault, live account endpoints or inference. No new delegates/tabs, no deploy/restart, no push/merge. Commit only your files with normal hooks; deliver commit SHA, executed commands/results and remaining integration contract in `result.md`. Coordinator cherry-picks into 279 and owns PR/runtime. Do not resume the unrelated editx assignment. This one-off assignment supersedes it for this idle seat.
