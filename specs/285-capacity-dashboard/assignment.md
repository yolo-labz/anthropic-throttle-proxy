# Consolidated quota and routing truth UI — workspace w1P

## Mission
Make the existing web UI a consolidated, truthful capacity view: every measured provider/account/seat visible; service connectivity must not imply usable quota. Keep HTMX/server-rendered architecture.

## Ownership
Work only in `/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-285-capacity-dashboard`, branch `285-capacity-dashboard`, base `2e7a43f`. Own `src/anthropic_throttle_proxy/ui/`, `fleet_ui_config.py`, UI tests and this spec directory. Do NOT edit `lanes.py`, probe, proxy/account/routing/config production code. The old `229-dashboard-truth` worktree contains unrelated WIP: leave it entirely untouched.

## Generic reproduction and contract
The existing screenshot shows subscription meter EXHAUSTED for a provider, but its live routing row says HEALTHY merely because DNS resolves; a new separately assigned Team subscription is absent because only the individual meter was shown. Reproduce with SYNTHETIC test data, not private screenshots/console/account records. Meter worker will preserve schema 1 and add `mimo:team-owner` alongside `mimo:plan`, kind=mimo, normal existing meter fields, distinct identity; use normalized lane rows without hard-coded singleton assumptions.

Show an at-a-glance summary of usable/exhausted/stale/unknown accounts and binding windows, per-seat used/remaining/reset, live inflight/queued/capacity and useful throughput where measured. Do not add unlike percentages/units/windows into a fictitious total or count unassigned seats as usable. Unknown and stale are not healthy. Distinguish TRANSPORT availability, subscription capacity and model eligibility. Copilot premium exhaustion must not falsely imply its separately unlimited chat/completion products are exhausted. Preserve progressive HTMX rendering, escape all captions, no new JS modules/dependencies, Catppuccin tokens.

Follow speckit spec→plan→tasks; smallest test-backed diff. Run focused UI tests, ruff and full pytest if feasible. No browser/account secrets/private Notes, live inference, deployment, new workers/tabs, push or merge. Commit only your files with normal hooks. Deliver SHA, executed tests and limitations in `result.md`. Coordinator cherry-picks into 279 and verifies actual runtime.
