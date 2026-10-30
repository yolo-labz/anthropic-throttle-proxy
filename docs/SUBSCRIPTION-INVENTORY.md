# Keeping the dashboard inventory current

The default board is an explicit operator inventory, not evidence that a subscription was paid or that a model is available. Start from docs/examples/fleet-ui-current.yaml after checking the account registry and current provider observations.

The existing FLEET_UI_CONFIG YAML is reloaded when its timestamp changes. Its optional defaults.hidden_subscriptions list hides exact row id or lane_id values from the presentation only. Hiding a retired desktop source does not hide active MiMo seats. It does not remove raw readings, Prometheus metrics, credentials or routing permission. A failed read, exhausted quota or expired OAuth token is not proof of subscription cancellation; keep that source visible until retirement is independently established.

Use show_primary: false when the process is only the dashboard and its primary inference lane has been retired. Its default status then says SUBSCRIPTIONS rather than displaying that retired lane's CRIT as a fleet verdict. Explicit workload selections remain independent, except that a hidden destination cannot be revived through a workload query.

`defaults.hidden_providers` hides exact destination IDs/names (case-insensitive) from Live routing, Registered providers and workload choices, including HTMX polls. It is separate from `hidden_subscriptions`: hiding Codex B must not hide Codex A's provider. Name the registry ID and sibling name explicitly when they differ; unknown future names remain visible by default. This is board membership, not a cancellation verdict. Raw collection, metrics and routing are unchanged. Older packages may validate but ignore this newer key: inspect the running package before claiming all surfaces are filtered.

The current example restores observed Anthropic sources without hardcoded bearer hashes. Keep real Claude holds and Copilot's spent premium allowance visible; Student entitlement is separate from premium usage. Codex C remains in the catalogue when native enrollment excludes it, not counted as available capacity. Z.AI/OpenCode are inactive for this board, not established billing cancellations; legacy MiMo rows are explicitly hidden rather than deleting history. Preserve operator preferences when adapting the example.

Do not fix stale plan captions by guessing a new tier. Omit the configured plan annotation and let the live observation speak. Free account headroom is not proof of paid frontier-model access. Consumer app memberships, one-time credit banks and API balances are not interchangeable subscription capacity.

Keep historical retirement and billing evidence in the corresponding private vault project note, not in the public example or test fixtures. Configuration is reversible by restoring the previous YAML; source rollback is a single revert PR.
