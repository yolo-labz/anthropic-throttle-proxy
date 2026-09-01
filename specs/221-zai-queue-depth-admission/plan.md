# Plan: queue-depth admission + honest queue-timeout Retry-After

Generator family: **anthropic** (Opus 5). Adversarial/done gate: **Codex Sol**
(different family), per the repo's mandatory review rule.

## Hypothesis

One sentence: the fair queue parks requests it can prove it cannot serve,
because admission bounds elapsed wait but never queue depth against the lane's
own service rate, and the queue-timeout `Retry-After` is a constant unrelated
to drain time.

Falsifier: a deterministic replay of the measured shape (2 slots occupied by
long generations, 3 queued, short budget) already rejecting pre-queue with a
truthful retry interval on current `main`. **Run 01/09/2026 — not falsified**
(see `evidence.md`): the arrival was parked, burned the whole budget, and got
`Retry-After: 5`.

## Where the change goes (one producer slice)

The single shared seam is `FairBearerLimiter` + `_FairSlotContext` in
`src/anthropic_throttle_proxy/limiter.py`. Every hot-path caller reaches the
queue through `limiter.slot(...)` (`proxy.py:3826`); `acquire`/`release` have
no other in-tree caller. Fixing the shared seam once covers all three
queue-timeout call sites in `proxy.py` (zero remaining budget, expired
deadline, elapsed wait) without touching the handler's control flow.

### 1. Service-time evidence (limiter)

Dispatch and completion already funnel through three `inflight += 1` sites and
two `inflight -= 1` sites inside the limiter. Each dispatch opens a per-slot
**lease** (`_holds: lease -> (lane, monotonic start)`) and each completion
returns its own lease, appending that request's duration to a bounded sample
deque.

- Leases, not arrival-order pairing. Completion order routinely differs from
  dispatch order, and pairing by order lets a stream of short completions pop
  the long holder's stamp — erasing the "this slot has been held for minutes"
  signal the incident turns on (Codex round-1 BLOCKER).
- The lease rides the dispatch future's result alongside the effective lane, so
  `acquire_lease` / `release(lease=)` / `_cancel_cleanup` all return the exact
  slot. `acquire()` stays as a shim for callers that release without one; those
  fall back to the oldest hold in the lane. An UNKNOWN lease takes nothing —
  popping "something else" would price a live request as finished.
- Cancellation cleanup returns the lease **without** recording a sample: a slot
  that was dispatched and immediately cancelled never ran upstream and must not
  poison the estimator with a ~0 s sample.
- Holds are bounded by real in-flight count, with a soft cap as a leak guard
  only (invariant: nothing in this process may grow unbounded — see #205).

### 2. Conservative estimate

`service_time_s = max(base, longest currently-held slot elapsed)` where
`base = p90(samples)` once at least `_MIN_SERVICE_SAMPLES` (3) completions
exist, else `config.QUEUE_DRAIN_DEFAULT_S`.

The inflight term is the load-bearing part for the measured incident: at
14:20 the two long generations had not completed, so a completion-only
estimator would have been blind to them. Elapsed hold time is a hard lower
bound on that request's service time — evidence, not a guess.

### 3. Admission math (revised twice under adversarial review)

Position is the **round-robin** rank, not raw depth: a client's next request
dispatches on its turn `own + 1`, by which time each sibling ahead of it in
`_rr_order` has had at most `own + 1` turns and each sibling behind it at most
`own`. Counting total depth would refuse a fresh client stuck behind a chatty
client's backlog — traffic the fair queue exists to serve promptly.

The wait is then a scheduled estimate rather than round arithmetic. Each server
is modelled as "free at T" (idle slots now; a held slot after its own residual:
`typical - age`, or a full service estimate once it is overdue), requests take
the earliest free slot, and the arrival starts at the `ahead + 1`-th such
event. Closed-form rounds cannot express an uneven schedule — two slots aged
200 s and 34 s free at very different times, and one scalar for the pair errs
in both directions. Inverting the same simulation over the horizon gives the
largest admissible `ahead`, which is what `/__throttle/admission` publishes.
Both loops are bounded at 1024 simulated dispatches.

### 4. Rejection path

`_FairSlotContext.__aenter__` computes the estimate before `acquire()` and
raises the existing `QueueWaitTimeout` (extended with `pre_queue` + the
estimate) when it does not admit. `proxy.handler`'s existing
`except QueueWaitTimeout` already dequeues the counter, finishes the probe, and
returns `_queue_wait_timeout_response` — so the marker header, anti-spoof
stripping, pushback-retry exemption, and AIMD exemption are inherited
unchanged.

Guard is inert when `queue_enabled` is false or `max_wait` is falsy — the
existing `if self.max_wait and self.limiter.queue_enabled` gate already
expresses exactly that condition.

### 5. Retry-After (proxy)

`_queue_wait_timeout_response` takes the estimate from the raised
`QueueWaitTimeout` (or recomputes it for the pre-dispatch call sites) and emits
`min(QUEUE_RETRY_AFTER_MAX_S, max(QUEUE_TIMEOUT_RETRY_AFTER_S, ceil(wait_s)))`.

The ceiling is a HARD cap, so a budget larger than the cap is answered with the
cap. Only the PRE-QUEUE refusal additionally raises the floor to the refused
budget (`budget_floored_retry_after`), because that request never waited and
would otherwise walk straight back into the same wall. A request that already
SPENT its budget waiting is not charged for it twice — that time is behind it,
and a slot may free the moment after the timeout (Codex round-2 MAJOR).

### 6. Published contract (proxy)

`FairBearerLimiter.snapshot()` gains a `drain` sub-dict computed against
`config.QUEUE_MAX_WAIT_S` for a NEW client. The published bound is a closed
form over the same schedule (each server free at `t` contributes
`floor((horizon - t) / service) + 1` starts), not a simulation: a step cap
would both break invariant #4 on a wide registry and understate real capacity
by orders of magnitude (Codex round-4 MAJOR).
 `_lane_saturation` aggregates it across measured
usable bearers into `saturation.queue_admit_max_depth` (sum, matching the
existing `slots`/`free`/`queued` sums) and `saturation.queue_admit` (max
service time, total samples, worst provenance, configured max wait). With no
measured bearer the block falls back to a config-derived cold bound so the
published value stays positive and its provenance reads `config`.

## Config surface (two env knobs, no hot-tune registration)

| Knob | Default | Meaning |
|---|---|---|
| `THROTTLE_QUEUE_DRAIN_DEFAULT_S` | `10.0` | Cold service-time estimate before enough completions exist. |
| `THROTTLE_QUEUE_RETRY_AFTER_MAX_S` | `300` | Ceiling on the advertised queue-timeout `Retry-After`. |

The guard's off switch is the existing `THROTTLE_QUEUE_MAX_WAIT_S=0` /
non-queue mode — no third knob.

## Risk + rollback

- **Risk: over-rejection on a fast lane.** Mitigated by trusting measured p90
  as soon as three completions exist rather than flooring at the cold default,
  and by the inflight term only ever *raising* the estimate with real elapsed
  evidence.
- **Risk: cold start rejects a legitimate first burst.** With no history the
  cold default (10 s) and the free-slot term keep a fresh lane permissive; the
  guard cannot fire at all while a slot is free and the queue is empty.
- **Rollback:** one `git revert` of the squash commit; the feature has no
  persisted state, no migration, and no consumer contract removal (it only adds
  keys under `saturation`).

## Verification

`specs/221-zai-queue-depth-admission/verify.sh`: targeted tests → full pytest
with max parallelism when available → ruff check/format → a contract assertion
that `saturation.queue_admit_max_depth` is a positive int on both an idle lane
and a lane with no bearers.
