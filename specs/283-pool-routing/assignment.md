# Static pool routing and useful parallelism — workspace w1P

## Mission
Fix the shared account-selection path so configured usable seats receive work fairly and retired/exhausted credentials cannot win just because they look unloaded. Reuse existing pool and `least_loaded`/`budget_paced` machinery.

## Ownership
Work only in `/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-283-pool-routing`, branch `283-pool-routing`, base `2e7a43f`. Own production `proxy.py`, `accounts.py`, `routing.py`, `config.py` and existing account/routing tests as needed. Do NOT edit `lanes.py`, probe script, UI files, or the new independent acceptance test file. Smallest justified diff; do not touch all owned files unnecessarily.

## Observed failure, sanitized
A static-key slot was atomically changed from retired credential A to usable B. B was serving successfully. A request carrying A still went upstream as A and failed quota, while B was busy. A request carrying B succeeded. Hypothesis: an unconfigured incoming bearer is still a selection candidate, and its apparent low occupancy can beat the configured pool. Falsify with a targeted failing test before changing anything. Preserve existing OAuth fallback semantics and explicit account pins; scope to configured static pool as warranted. Missing/malformed pool material must not leak raw secrets or silently pretend there is capacity.

## Acceptance
- Prove retired incoming A cannot bypass a usable configured static pool B, including B already in flight.
- Multiple configured eligible seats: symmetric load does not concentrate arbitrarily; compare normalized occupancy/capacity, preserve request-safe failover and model compatibility.
- Exhausted/rejected/cooldown/stale cases stay excluded according to authoritative evidence; no inference of fresh quota from a new hash, no reset of budgets.
- Existing central/local behavior, cancellation and hard caps preserved. Do not blindly increase caps or retry storms. State any inability to bind per-seat meters as an integration gap, not invented proof.
- Tests exact path, targeted suite, full pytest, ruff. No live calls, secrets, rbw/browser, private vault, service changes, additional workers/tabs. Do not alter Pi or reserved Z.AI slots.

Follow speckit spec→plan→tasks; record hypothesis and falsifier. Commit only your scoped files normally; no push/merge. `result.md` must name SHA, test outputs, known limitations. Coordinator integrates into 279 and handles runtime. This assignment replaces the completed editx task in this idle seat.
