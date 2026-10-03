# Prospective runtime configuration

The default `THROTTLE_PROSPECTIVE_MODE=off` creates no accounting owner and ignores
`THROTTLE_PROSPECTIVE_CONFIG`. To opt in, set mode to `observe` or `strict` and
point that second variable to an absolute manifest path. The file is read once,
before the listener and background probes start; there is no hot mode switch.

The following is a **synthetic schema example**, not provider authority or a
production budget:

```json
{
  "sources": ["fixture-source"],
  "endpoints": ["https://provider.invalid/v1"],
  "models": ["fixture-alias", "fixture-model"],
  "entries": [{
    "source": "fixture-source",
    "endpoint": "https://provider.invalid/v1",
    "alias": "fixture-alias",
    "upstream": "fixture-provider",
    "account": "fixture-account",
    "model": "fixture-model",
    "budgets": {"max_requests": 2, "max_tokens": 1000},
    "output_default": 20
  }],
  "state_directory": "/var/lib/example-prospective-state",
  "allow_cold_start": false,
  "max_pending": 4,
  "wait_timeout": 1.0,
  "budget_label": "fixture-budget",
  "retry_after_s": 5
}
```

`source` must equal the operator account label returned by the actual credential
selection; it is never inferred from a bearer hash or request header. Endpoint
matching is exact against the configured upstream base. Every alias and canonical
model must appear in the catalog. Aliases for one canonical scope must agree on
budgets and output default. The public budget label must contain no secret or
account-derived identifier. Invalid or ambiguous configuration refuses startup.

`state_directory` must be writable by this process and exclusively owned by its
operator. The runtime creates distinct `observe` and `strict` directories with a
nonblocking lifetime lock. Existing symlink/hardlink aliases are rejected. This
coordinates cooperating local writers, not separate machines or a malicious
writer with the same user identity. Never delete or copy a ledger to make room
for a new owner. A failed close retains custody until process exit. The original
startup failure stays primary if cleanup also fails.

Cold-start permission is explicit: use `true` only for a deliberately new ledger
whose full budget is valid. Missing/corrupt state with permission false cannot
silently reset spent capacity. Turning off enforcement preserves state, but
traffic while off is absent from that debt; re-enabling strict requires a safe
window/calibration decision, not merely reusing an old file.

Only direct, supported text/function-message bodies with a configured output
bound can be accounted. Unknown scope/body, relay topology and internal probes
refuse locally under strict. Probe refusal is inconclusive and does not
quarantine or recover credentials. Observe preserves transport and counts these
as unknown. Its bounded shadow sample can drop work and is **not** complete
provider usage or estimator calibration. Fixed-label Prometheus counters expose
local strict refusals and observation outcomes separately from provider feedback.

Production remains off until T005 establishes complete demand and estimator
error for one full day. T006 is then a separate MiMo-only strict canary with the
ten falsifiers green and retained rollback/debt evidence. Local admission does
not eliminate provider429s or establish a vendor token guarantee.
