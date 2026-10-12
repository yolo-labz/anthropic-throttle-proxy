# Keeping the dashboard inventory current

The default board is an explicit operator inventory, not evidence that a subscription was paid or that a model is available. Start from docs/examples/fleet-ui-current.yaml after checking the account registry and current provider observations.

The existing FLEET_UI_CONFIG YAML is reloaded when its timestamp changes. Its optional defaults.hidden_subscriptions list hides exact row id or lane_id values from the presentation only. Hiding a retired desktop source does not hide active MiMo seats. It does not remove raw readings, Prometheus metrics, credentials or routing permission. A failed read, exhausted quota or expired OAuth token is not proof of subscription cancellation; keep that source visible until retirement is independently established.

Use show_primary: false when the process is only the dashboard and its primary inference lane has been retired. Its default status then says SUBSCRIPTIONS rather than displaying that retired lane's CRIT as a fleet verdict. Explicit workload selections remain independent.

Do not fix stale plan captions by guessing a new tier. Omit the configured plan annotation and let the live observation speak. Free account headroom is not proof of paid frontier-model access. Consumer app memberships, one-time credit banks and API balances are not interchangeable subscription capacity.

Keep historical retirement and billing evidence in the corresponding private vault project note, not in the public example or test fixtures. Configuration is reversible by restoring the previous YAML; source rollback is a single revert PR.
