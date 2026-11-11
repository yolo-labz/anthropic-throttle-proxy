# Revalidate Claude subscription capacity after a reset

A stored multi-day Retry-After can outlive a subscription reset or plan change.
On 11/11/2026 both configured accounts had fresh Ultra allowance, while the local
gateway continued refusing them using the previous day's exhausted readings.
An authenticated Browser Fleet MCP usage page and successful same-model
subscription messages established the current capacity. Clearing only those
confirmed-stale holds restored normal Pi requests and both-account routing.

Use the registered server browser identity to inspect the current account and
usage page. A rejected OAuth usage endpoint is not a Messages entitlement check.
Preserve the exact provider/model selected by the user; a quota reset does not
authorize a different model or API billing.

The repository diagnostic reads only `claudeAiOauth` credentials and makes one
small message per supplied account. It never uses an API key, retries, changes
models, follows redirects or prints credentials:

```sh
python3 scripts/claude-subscription-check.py \
  --model claude-opus-5-5 \
  --account "gmail:$HOME/.claude/.credentials.json" \
  --account "proton:$HOME/.claude-proton/.credentials.json"
```

Pass the currently selected exact model if it differs from the example. Genuine
success requires an assistant message from that exact model with nonempty text.
The output contains fresh provider usage headers and a short bearer hash.

When the current subscription check contradicts old local holds, add
`--release-verified-holds` to the same command. This explicitly quiesces admission
**before** checking accounts, refuses unknown/busy state or any failed account,
then stops the canonical gateway and its active socket at an idle boundary.
It backs up the state with restricted permissions and atomically removes only
successfully checked bearers' holds. Unrelated holds remain. The service and
previously active socket are restored, and admission is reopened on exit.
There is no automatic bypass of a genuine provider limit.

Verify a normal Pi turn afterward and inspect `/__throttle/health`: successful
messages should refresh the unified reading, both accounts should serve work,
and their stale Retry-After/half-open gates should be cleared. Resume unfinished
panes through `herdr-prompt` without overwriting drafts or changing models.

The recovery reports the backup path. If a state write needs to be reversed,
quiesce and drain the same service/socket before restoring that backup. Avoid
restoring an old exhausted verdict over newer provider evidence. The procedure
does not alter credentials, gateway configuration or installed package versions.

Regression checks:

```sh
uv run pytest -q tests/test_claude_subscription_check.py
uv run ruff check scripts/claude-subscription-check.py tests/test_claude_subscription_check.py
```
