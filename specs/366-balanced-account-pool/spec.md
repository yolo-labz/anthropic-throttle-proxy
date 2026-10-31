# Balanced account rotation

## Outcome
Distribute Claude-lane work equally across eligible subscription accounts and make the allocation visible in the Web UI. Equal-size work alternates accounts; unequal response sizes are compensated using observed input, cached-input and output tokens. Equal request counts alone are not equal usage. Fairness is local observed token work since service start, not a promise of equal provider billing-window percentages or cost.

## Acceptance
- Opt-in round-robin mode retains existing credential, quota, scoped-model, retry/probe and API-key boundaries; existing modes are unchanged.
- Equal-work sequential and concurrent arrivals rotate; a large measured response causes lighter accounts to catch up. Unknown usage retains an explicit estimate instead of becoming free work.
- Read-only status/dashboard calls never advance rotation. Accounts joining/rejoining accrue no credit for time unavailable. Token rotation preserves the configured account's identity.
- Queue cancellation/local admission refusal never debits upstream work. Sent/unknown outcomes remain accounted. Quota-held accounts are skipped, never probed to manufacture a 50/50 graph.
- The HTMX UI shows policy, eligibility/hold reason, per-account attempted-request and observed-token shares, unknown/estimated usage, and the accounting epoch. Empty history is unknown, not 0% balance. Hidden local/account surfaces stay hidden.
- Regression tests, full pytest and ruff pass. Deployment identifies the exact package and verifies a live sentinel response and server-rendered account distribution without disrupting active requests.
