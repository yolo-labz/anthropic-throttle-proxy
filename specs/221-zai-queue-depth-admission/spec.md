# Spec: queue-depth admission + honest queue-timeout Retry-After

## Problem

The fair queue bounds how long a request may WAIT, never whether the wait is
possible. A request is parked whenever a slot is busy, however deep the queue
already is and however slow the lane's requests actually are. When the bound
expires the proxy answers `503` with a hardcoded `Retry-After: 5`.

Measured 01/09/2026 14:20–14:24 BRT on the live `:8766` Z.AI lane:
`max_concurrent=2`, queue depth 3, inherited effective wait budget 30 s, and
the two requests holding the slots completed after **113.7 s** and **221.2 s**.
The arriving request therefore could not reach a slot within 30 s *by
construction* — it needed two service rounds behind three queued peers. The
proxy still admitted it, burned the full budget in silence, emitted the
`queue-wait-timeout` 503, and advertised a 5 s retry that is off by roughly two
orders of magnitude. The client (Pi) retried the same lane four times and
aborted. Live health later showed queue/inflight 0: this was saturation, not a
dead proxy and not an upstream quota wall.

The lane's own numbers were sufficient to know the request was hopeless before
it was ever parked.

## Requirements

- **FR-1 — Depth admission.** Before parking a request in the fair queue,
  reject it when the estimated time to reach a slot exceeds this tier's
  effective max wait. The estimate uses only live lane facts: the live slot
  count, each held slot's own modelled remaining time, the arrival's
  **round-robin** position (not raw queue depth — a new client overtakes a chatty client's backlog by design, and
  counting the whole depth would refuse traffic the fair queue exists to serve
  promptly), and a conservative recent service-time estimate. Accepted requests
  keep the existing per-client ordering; rejection happens strictly before
  enqueue.
- **FR-2 — Published bound.** `/__throttle/admission` publishes a positive
  integer `saturation.queue_admit_max_depth` plus the estimator inputs
  (`saturation.queue_admit`: service time, sample count, provenance, slots,
  free slots, and the max-wait used). Missing or insufficient history must not
  claim measured evidence it does not have: provenance is explicit and the
  bound stays finite and conservative. The published horizon is the
  **configured** `THROTTLE_QUEUE_MAX_WAIT_S` and the arrival is assumed to be a
  NEW client; a request inheriting a shorter end-to-end budget is judged
  against that shorter horizon on the hot path, so `max_wait_s` states which
  horizon the published bound describes. The endpoint stays cheap — no I/O, no
  new locks, `/__throttle/health` remains < 50 ms (invariant #4).
- **FR-2a — Bypass lanes.** A bearer in `off`/`observe` mode enforces no queue
  admission at all; the published block must say so (`enforced: false`,
  `bypass_bearers`) rather than advertise a fair-queue bound it never applies.
- **FR-3 — Truthful Retry-After.** The queue-timeout `503` carries a bounded
  integer drain estimate instead of the constant `5`, never shorter than the
  historical 5 s floor. A request refused **before** it waited is additionally
  floored at the budget it was just refused against — otherwise the retry
  re-enters the same wall immediately, which is exactly the observed ×4 retry
  storm. A request that already **spent** that budget waiting is not, because
  that time is behind it and the lane may free a slot the instant after the
  timeout. The configured ceiling is a HARD cap in both cases: an unbounded
  hint is not actionable.
  *(Amended after Codex round 1, which showed the original single floor made
  the "ceiling" non-binding for budgets above it and charged already-spent
  waiting time twice. Recorded in `evidence.md`.)*
- **FR-4 — Preserve.** Queue-timeout marker header and its anti-spoof
  stripping, pushback-retry and AIMD exemptions for queue timeouts, limiter
  cancellation cleanup (no slot or counter leak), per-client fairness, the
  priority reserve lane's dedicated pool, and every existing default for
  non-saturated traffic are unchanged. The guard is inert whenever the queue
  is disabled (`off`/`observe`) or the effective max wait is unset/0.
- **FR-5 — Cost.** No new dependency, no vendor SDK, no credential handling
  change, no new log field carrying a token or client payload.
- **FR-6 — Evidence.** A RED→GREEN test drives the real limiter/admission seam
  with the measured 2-slot / 3-deep / short-budget shape, plus cold history,
  disabled bound, priority reserve, cancellation cleanup, and Retry-After
  bounds.

## Non-goals

- No consumer change. Pi's retry policy, the NixOS pin, and host activation are
  separate slices; this repo ships the producer only.
- No change to `allow` in `/__throttle/admission`. Saturation has never been a
  go/no-go verdict and does not become one here.
- No new upstream pacing, AIMD, or routing behavior.

## What this is not

The estimate is a conservative admission **policy**, not a proof. A holder's
age bounds its total service time from below, never its remaining time — it may
return in the next millisecond. The model deliberately errs toward
over-estimating the wait (a false refusal answered in milliseconds, which the
SDK retries) rather than under-estimating it (a request parked past the
client's patience, answered too late to be useful). False refusals are an
accepted cost, which is also why enforcement requires evidence rather than a
guess.

## Acceptance

- The deterministic replay of the measured shape rejects **before** enqueue,
  with `queued_total` unchanged and no slot consumed.
- The rejection's `Retry-After` is ≥ the exhausted wait budget and ≥ the 5 s
  floor, an integer, and ≤ the configured ceiling.
- `/__throttle/admission` exposes a positive `saturation.queue_admit_max_depth`
  on an idle lane and on a lane with no bearers observed yet.
- Full `pytest`, `ruff check`, and `ruff format --check` pass; `verify.sh` is
  green.
