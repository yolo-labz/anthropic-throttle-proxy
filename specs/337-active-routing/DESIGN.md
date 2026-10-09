# Active routing — design (spec 337)

Operator directive (Pedro, 09/10/2026 15:36): *“we need to improve the 🚦 Throttler
Platform to something more responsive and that works on managing the cost problem, we
need active routing and better solutions”* — with *“i would like to not throttle”*,
*“spread the usage on more keys”*, *“my agents at full force all the time”*.

## Problem

Lane selection today is **static**: `select_lane` walks the role's fixed chain and takes
the first open lane. Failover is reactive spill (`_select_lane_once` + saturation stamps,
`_SPILL_ON_429_ROLES`), so a lane that is open but *slow, capped or expensive* keeps
draining traffic until it stamps saturation. Under a burst the fleet serialises behind
the chosen lane instead of spreading across the fleet's measured capacity and keys.

## Solution: rank every (lane × credential) candidate per request

One new module, `active_routing.py` — pure decision logic plus a small state table.
Everything else is reused in place.

1. **Candidate set** — for the request's role: every chain lane that passes the existing
   hard filters (`lane_usable`, `bearer_usable`, credential-class), each × its usable
   credentials (proxy-owns-key lanes contribute one synthetic candidate). Filters stay
   exactly as strict as today; ranking only ever reorders survivors.
2. **Ranking** — weighted score from measured state, all three axes visible in one number:
   - *capacity headroom* — `1 − util` from unified 5h/7d gauges and plan meters
     (`lanes.py`), plus the lane's in-flight count;
   - *health* — EWMA of observed 429/5xx/connection-error rate per candidate, and the
     cooldown table below;
   - *cost* — per-turn USD estimate for the request's role/model from the ledger's
     observed cost-per-token, so equal-capacity candidates prefer the cheaper one.
   Ties break by (headroom, health, cost) lexicographically — cost never overrules a
   hard capacity or health signal, it only breaks ties (cost-managing without gambling).
3. **Graceful failover, never a queue** — an observed 429/5xx/connection error puts the
   candidate in **cooldown** (Retry-After honoured, else short exponential), and the
   *same request* fails over to the next candidate, bounded by `max_attempts` (default 3
   hops) and the request's existing end-to-end wait budget. A request is never parked to
   wait for a specific candidate — `queue_mode` and the fair-queue safety net are
   untouched (the throttler is a safety net, not a gate).
4. **Cost accounting per lane/seat** — every completed request writes one ledger row:
   `(ts, lane, seat, role, model, attempts, latency_ms, input/output tokens, usd)`.
   Prometheus counters: `throttle_spend_usd_total{lane,seat}` and
   `throttle_request_latency_seconds{lane,hop}` — “what is being spent, by whom”.
5. **Responsiveness budget** — ranking is in-memory and bounded (<1 ms p99 target);
   failover costs ≤ 1 extra hop per attempt; `throttle_failover_hops_total` and an SLO
   breach counter (`selection > 5 ms`) make the latency budget observable per hop.

## Reuse map (no new stack, no new ports)

| Piece | Reused from |
|---|---|
| lane/bearer usability, chain, lane state | `routing.py` (`lane_usable`, `bearer_usable`, `select_lane` fallback) |
| measured gauges (util, plan, billing, reset) | `lanes.py` meters + `unified_live_view` |
| spill/failover loop, saturation stamps | `ingress.py::_select_lane_once` (ranking feeds it; spill semantics unchanged) |
| per-request cost rows, budgets | `ledger.py` (`LaneLedger`, `Budgets`) |
| counters/histograms | `metrics.py` |
| credential state, `least_loaded` | `accounts.py` (read-only; candidate enumeration) |

Ports unchanged: ingress `:8760`, gateway `:8765`, GLM `:8766`, Kimi `:8767`, plus the
existing lane registry. No parallel router service.

## Constraints honoured

- **No queueing as primary mechanism** — selection never waits; the existing fair queue
  stays a bounded safety net with its knobs untouched.
- **MiMo wallet gate intact** — the 300 s freshness receipt fence in the gateway hot path
  is not modified by this slice.
- **No fabricated identities/accounts** — candidates come only from configured lanes and
  real credentials already tracked by the runtime.
- **Secrets** — no key material in code, tests or evidence; credential handles only.
- **Default-off activation** — `INGRESS_ACTIVE_ROUTING=off|on` (default `off`); landing
  source + tests here is not a runtime activation. Rollout is root's, like 2719/2720.

## Measurement plan (deliverable 2)

Baseline (before): per-lane throughput (served/inflight), latency histograms, 429/5xx
rates and cost-per-successful-turn read from the live lanes' `/__throttle/health` +
`/metrics` — recorded under `specs/337-active-routing/evidence/`. After: same readout with
`INGRESS_ACTIVE_ROUTING=on` plus a burst test (async burst against fake lane upstreams,
plus `load/k6-real-world.js` shape) proving requests spread across candidates instead of
stalling behind one lane.

## Non-goals (this slice)

Central-queue redesign, credential *minting* (w1P:p1F console seat), NixOS lane config
(w1W:p2B, PR #2730), and any provider-facing change — those stay with their owners.
