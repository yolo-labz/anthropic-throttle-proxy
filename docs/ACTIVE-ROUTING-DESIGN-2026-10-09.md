# Active Routing — design (2026-10-09)

Operator directive (Pedro, 09/10 15:36): *"we need to improve the 🚦 Throttler
Platform to something more responsive and that works on managing the cost
problem, we need active routing and better solutions"* — with prior asks *"i
would like to not throttle"*, *"spread the usage on more keys"*, *"my agents at
full force all the time"*.

**Thesis:** the platform's job is to *route*, not to *hold*. Every request should
land on the healthiest, cheapest willing lane within its latency budget; a wait
is what happens only after no better target exists. Queueing/limits stay as the
safety net that protects accounts — never as the mechanism that schedules work.

## Scope

The platform itself (gateway :8765, central :8766, ingress :8760, MiMo lanes
:8773/:8774). No new parallel stack. The MiMo wallet gate (300 s receipt
freshness) is intact and untouched; secrets stay in rbw / 0600 files; no
fabricated identities or accounts.

## Pillars

1. **Active routing (per-request selection).** Rank candidate credentials by
   measured **capacity** (live limiter/unified windows), **health** (Retry-After
   state, pushback recency, retry-probe probation) and **cost** (budget pace for
   subscriptions, USD/turn for PAYG). Existing scorer
   (`_routing_pressure_score`) already prices capacity+health; cost enters as a
   first-class term (see 3).
2. **Graceful failover — 429/5xx without serialising.** Today a pushback
   429/503/529 that is *not* an armed-long-Retry-After case AIMD-shrinks,
   **waits the pause and retries the same bearer**. Change: hand the request
   back to the routing gate (the existing `_RetryAfterArmed` re-entry) so it
   dispatches on a healthier sibling *immediately*; the same-bearer wait is kept
   only as the none-better fallback (limiter honors the latched window before
   the next dispatch). Ordering invariant preserved: keepalive-hold FIRST for
   streaming transients, pushback second; entitlement refusals and queue-timeout
   503s remain exempt; single-account mode unchanged. 5xx/transport recovery
   (central-down → direct) is unchanged in this slice; cross-account failover on
   hard 5xx is a follow-up (see Slices).
3. **Cost accounting per lane/seat.** The fleet spends across Anthropic
   (subscription), MiMo PAYG ($0.435/$0.87 per M), Z.AI and GPT lanes, yet the
   proxy's USD metric (`anthropic_cost_usd_total`) is Anthropic-rate-only with
   no seat dimension. Add a rate table extension +
   `throttle_seat_cost_usd_total{seat,lane,model}` + `cost per successful turn`
   in `/__throttle/health`, so "what is being spent, by whom" is answerable per
   lane and per seat.
4. **Responsiveness budget (latency SLO per hop).** Instrument per-hop latency
   (client→slot-acquire, slot→upstream-first-byte, upstream→last-byte) as
   `throttle_hop_latency_seconds{hop}`, publish p50/p95 per hop against a
   configured SLO (`THROTTLE_HOP_SLO_MS`, warn-only), and report budget
   violations in health. Measurement, not enforcement, in the first slice.

## Non-goals

- No new queue, gate, or admission layer. `FairBearerLimiter` / prospective
  admission stay the safety net; their semantics are not reworked here.
- No changes to entitlement-refusal, queue-timeout, or keepalive-hold contracts.
- No provider SDKs on the hot path (invariant 1); no token ever logged
  (invariant 2).

## Slices (PR sequence)

| Slice | Content | PR |
|---|---|---|
| S1 | **Pushback failover via the routing gate** (pillar 2) + burst harness + before/after measurement | this PR |
| S2 | Cost accounting per lane/seat + cost-per-turn surface (pillar 3) | next |
| S3 | USD-cost term in the router score + per-lane ingress selection (pillar 1 cost term) | then |
| S4 | Per-hop latency SLO instrumentation + health budget report (pillar 4) | then |
| S5 | Hard-5xx cross-account failover; lane-level health fan-out via `:8760` ingress chains | later |

## Measurement plan

`load/burst_probe.py` — bounded burst (N concurrent tiny requests) recording
per-request status, TTFB, total, queue-wait markers and Retry-After; emits JSON.
Baseline (before) and post-change (after) runs compare: p50/p95 total latency,
429 relay rate, same-bearer stall count (`rate-pushback-retry` log lines with
equal bid) vs failover count. A "no stall" proof = a burst against a lane whose
first-choice credential pushes back completes on the sibling without the
turn-level wait the old same-bearer retry imposed.

## Risk notes

- The `_RetryAfterArmed` re-entry contract is reused verbatim (it already
  implements "reroute onto a healthy bearer, fast-fail only when none is
  better"); the only new behavior is *raising it in the generic pushback case*.
- AIMD/metrics accounting stays single-shot: the raise happens *before*
  `_aimd_feedback`, and the handler's `_finalize` performs it exactly once
  (same as the existing armed-Retry-After path).
- Real **and synthetic** pause windows are latched on the failing limiter
  before hand-back: a real `Retry-After` cannot be instantly re-probed, and an
  unlatched synthetic pause could ping-pong between two pushing-back bearers
  past the per-call `RATE_PUSHBACK_RETRIES` budget (which resets on every
  `_forward_with_retry`). The none-better fallback therefore waits the pause
  before the same-bearer retry — which is exactly the cooldown the synthetic
  pause exists to represent (2 s concurrency / budget backoff), and a
  safety-net wait, not a schedule.
