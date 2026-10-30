# 368 — Current provider board

Date: 2026-10-30

## Scope

One UI-data/config slice. No CSS/templates, runtime package activation, service restart, credential or routing changes. Preserve hero facts sizing and truthful unknown/stale/hold readings.

## Hypothesis and falsification

The old mutable YAML hides restored Anthropic accounts and seeds legacy subscriptions; display projection leaves sibling providers/workload choices visible even when the operator excludes them from the board. Falsified if the old artifact passes the exact ghost-destination regression or if raw observations change after projection.

## Contract

- Existing exact `hidden_subscriptions` remains per-source, not per-provider.
- Optional `hidden_providers` names destination IDs/names explicitly, case-insensitively, across registry, Live routing and workload choices. A query cannot revive a hidden workload. Unknown future providers remain visible.
- No inferred cancellation from quota, refusal, ineligibility or connectivity. Held Claude and exhausted Copilot remain represented; Student entitlement is independent of premium allowance. Unenrolled Codex sources remain catalogue-only.
- Anthropic sources come from live accounts; no hardcoded bearer hashes or plan/model captions.
- Projection never changes raw evidence, gauges, metrics, routing, credentials or holds.
- Full-page and HTMX-poll offline rendering use the same projection.

## Runtime boundary

The existing deployed package understands subscription/family hiding but not `hidden_providers`. Validating YAML there is not evidence the new destination projection is activated. No package activation is authorized. Live HTML belongs to the Impeccable browser owner; this slice uses offline rendering and operational JSON only.
