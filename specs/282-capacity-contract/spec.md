# Spec 282 — Fair capacity policy and measurement (bounded contract)

Branch `282-capacity-contract` · base `2e7a43f` · supersedes the editx assignment.
Scope: this file, `plan.md`, `tasks.md`, and `scripts/check-seat-fairness.py` ONLY.
No production code changes, no deployment, no account access.

## 1. Problem

The fleet's stated goals — consolidate limits, equalize eligible seat load, improve
useful throughput per connection — are currently pursued with ad-hoc cap tuning. The
missing piece is not a new scheduler: `FairBearerLimiter` (per-client RR, AIMD,
bounded wait), `budget_paced`/`least_loaded` routing gates, plan meters and static
credential pools already implement the mechanics. What is missing is a **bounded
acceptance contract** for what "fair" and "more throughput" mean, and an
**executable measurement** that can falsify a tuning claim.

## 2. Definitions

- **Seat** = one routable credential/bearer (static pool entry or lane). Fields that
  matter: assignment (has a credential), window state (fresh / stale / rejected /
  unknown / exhausted), capability set (private, provider-family, model tier),
  reserved-lane membership.
- **Eligible seat** (E): `assigned ∧ window-state ∈ {fresh, allowed} ∧
  capability-compatible(request) ∧ not reserved-lane-exempt`. Seats that are
  **exhausted, stale, unknown or unassigned are NOT eligible** and never were.
- **Eligibility-normalized utilization** (target): a seat's share of completed work
  divided by its share of **eligible** slots. Equal raw requests across unequal
  capacities is NOT this: 6 jobs to a 2-slot seat and 6 to a 1-slot seat is an equal
  raw split (0.50/0.50) but a 0.75/1.50 normalized split.
- **Equal raw requests**: the current naive baseline (count-based split).

## 3. Accounting rules (hard — never negotiable for tuning)

| # | Rule | Rationale |
|---|---|---|
| C1 | Never **add percentages from different windows** (5h + 7d + scoped-weekly). | 40% of 5h + 60% of 7d is not 100% of anything. |
| C2 | Never **mix units**: usage credits, money and percentages are separate ledgers. | Paid credits say nothing about window pressure. |
| C3 | **Purchased-but-unassigned capacity is not active quota.** It may buy future eligibility; it never counts in today's denominator. | Unassigned seats cannot serve traffic. |
| C4 | 5h/7d percentages support **window-local** statements only. **No monthly-runway claim** is derivable from them. | Windows roll; resets differ per account. |
| C5 | Denominators and numerators use **eligible seats only**; excluded seats contribute to neither. | An exhausted seat's 0 work is not fairness. |
| C6 | Reserved lanes (Z.AI `PRIORITY_RESERVE_SLOTS`) and existing Pi behavior are **preserved** — never repurposed to equalize load. | Latency lane exists for evaluator survival. |

## 4. Fairness contract (asserted by the check)

| # | Invariant | Source of truth |
|---|---|---|
| F1 | Queued work interleaves **per-client round-robin**; no FIFO monopolization. | `FairBearerLimiter._next_waiter` |
| F2 | A chatty client's backlog cannot starve a sibling: while both clients have queued work, dispatch alternates 1-for-1; a sibling with `k` queued requests completes all `k` within the first `2k` dispatches. | same |
| F3 | An **unavailable seat receives zero work** (routing/eligibility exclusion is total). | contract §2 + `_bearer_routing_retry_after` gate family |
| F4 | **Cancellation never leaks**: a cancelled waiter never acquires; if it raced a dispatch, the slot and lease return; `inflight` and `_holds` return to baseline. | `_cancel_cleanup` + lease return |
| F5 | A bounded wait answers **`QueueWaitTimeout` with no slot held** (pre- or post-park), preserving the clean 503 + Retry-After shape. | `_FairSlotContext.__aenter__` |
| F6 | AIMD shrink **never empties the lane**: `max_concurrent ≥ AIMD_MIN ≥ 1` after any number of shrinks. | `limiter.shrink` + `config.AIMD_MIN` |

## 5. Throughput measurement contract

- T1 **A higher numeric cap is not proof of throughput.** A cap raise is admissible
  evidence only alongside measured **effective overlap** (mean concurrent service),
  completed-work/time, and queue-wait distribution from the same run.
- T2 **Falsifiable tuning targets** (raise cap / dispatch gap only if ALL hold in the
  measured window):
  1. effective overlap reached the current live cap at least once (seats were the
     binding constraint, not demand);
  2. queue-wait p95 > `GAP_TARGET_S` (default 0.25 s simulated / 30 s live proxy) or
     sustained queued depth > 0 while overlap < cap;
  3. zero 429s and zero `QueueWaitTimeout`s in the window;
  4. no C1–C4 accounting violation in the reading that justifies the change.
- T3 **Halt conditions** (stop tuning, revert/hold): ANY upstream 429; `QueueWaitTimeout`
  rate > 1% of requests in the window; overlap falls while cap rises (demand-bound,
  not seat-bound); any eligibility reading that mixes windows (C1).
- T4 Every synthetic result is labeled **simulation**; no provider benchmark, real
  inference, account access or cross-window extrapolation is implied.

## 6. Non-goals

No new scheduler framework; no changes to `src/`, `tests/`, lanes, credential pools,
deployment config or Nix; no claims about monthly runway; no browser/credentials/private
vault use. Coordinator integrates the commit to branch `279`.
