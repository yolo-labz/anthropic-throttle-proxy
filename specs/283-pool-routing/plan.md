# 283 — Static pool routing + useful parallelism — plan

Base `2e7a43f`, branch `283-pool-routing`, worktree
`~/Documents/Code/yolo-labz/anthropic-throttle-proxy-283-pool-routing`.
Source-only slice; no live calls, secrets, deployment or pushes.

## Hypothesis (assignment: "falsify with a targeted failing test before changing anything")

The shared selection path (`_account_route_decision` → `_account_selection` →
`_healthy_known_unconfigured_bearer`) lets an **unconfigured incoming bearer win
over a usable configured pool seat**:

1. the preservation gate compares **raw local occupancy** —
   `_bearer_local_load_score(incoming) <= best_configured_load` — so a
   retired/unconfigured bearer that "looks unloaded" (0 queued, 0 inflight)
   beats a busy-but-usable seat; the request keeps the retired credential and
   fails quota upstream. (Incident: static-key slot atomically rotated
   A→B; A-carrying request went upstream as A while B was busy/in-flight.)
2. seat-vs-seat ranking uses the same raw counters, so **asymmetric-capacity
   seats under symmetric normalized load concentrate** on the smallest raw
   load (the weakened/shrunken seat) instead of spreading.
3. `_healthy_known_unconfigured_bearer` never consults **credential
   quarantine**, so a dead-but-idle incoming credential can still be preserved.

## Falsifiers (RED before the fix, in `tests/test_proxy_helpers.py`)

- F1 `test_retired_incoming_cannot_bypass_usable_static_pool_seat` — static pool
  seat (bare `tp-…` file) busy (inflight=1) **and** idle variants; unconfigured
  incoming with fresh healthy evidence. Must rewrite to the seat. Today it keeps
  the retired incoming → RED.
- F2 `test_symmetric_normalized_load_spreads_across_seats` — two configured
  seats with live ceilings 2 and 8 held at equal **normalized** occupancy
  (1/2 vs 4/8). Repeated selections must spread. Today raw comparison picks the
  2-seat until it saturates → RED.
- F3 `test_quarantined_incoming_never_wins_on_idle_occupancy` — credential-dead
  incoming, healthy idle pool. Must route to the pool. Today preserved → RED.

## Fix (smallest justified diff, shared path only)

1. `accounts.py`: `_digest_cred` reports the credential shape
  (`auth_type`: `oauth`|`static`) and `account_snapshot`/`routing_snapshot`
  carry it. Static = bare token line or JSON `apiKey`/`token`/`accessToken`.
2. `proxy.py::_account_route_decision`: when the configured pool contains a
   **static seat**, skip the unconfigured-incoming preservation entirely — on a
   static pool only pool credentials work upstream (model/upstream
   compatibility), so an unconfigured carrier is retired/foreign pool material
   and may not bypass. OAuth-only pools keep today's semantics (FR-004
   "equally idle caller wins" untouched; explicit `x-api-key` pins untouched;
   api-key prefer/overflow + OAuth fallback untouched).
3. `proxy.py::_bearer_local_load_score`: compare **normalized occupancy** —
   scale the calibrated queue/inflight weights by `MAX_CONCURRENT /
   lim.max_concurrent` (the AIMD live ceiling = the seat's current capacity).
   Identical to today for same-ceiling fleets (the anti-dogpile invariant:
   per-inflight weight stays >= 10), so `budget_paced` pacing constants keep
   their meaning; shrunken-ceiling seats no longer attract raw-cheap load.
4. `proxy.py::_healthy_known_unconfigured_bearer`: reject a
   `_bearer_credential_dead` incoming — authoritative exhaustion evidence
   beats apparent idle occupancy.

Non-goals / recorded as integration gaps (result.md): per-seat upstream meters
are bound to cred **paths** (`_endpoint_cache[path]`) and lane reports, not to
bearer hashes, so a rotation A→B cannot carry meter evidence across the hash
change — the fix infers no fresh quota from a new hash and resets no budgets.

## Tasks

- [x] T0 read assignment + map selection path
- [ ] T1 write F1–F3, capture RED
- [ ] T2 implement fix (accounts auth_type, static-pool scope, normalized load, dead gate)
- [ ] T3 targeted + full pytest + ruff; confirm existing routing/statusline tests unchanged
- [ ] T4 commit scoped files; result.md with SHA, outputs, limitations
