# Prospective admission — authority, accounting and refusal contract

**Status: contract only.** No enforcement is implemented by this slice. The
predecessor is 244, which shipped six red assertions and three positive controls
showing that the current admission bounds *time and slots*, never *tokens*.

## Why a contract comes first

MiMo publishes 100 RPM and 10M TPM aggregated per account and model, ships no
bucket headers, and does not document a concurrency ceiling. Today the lane
fair-queues, paces bursts, holds a bounded wait and shrinks on pushback — all
correct, and all blind to how many tokens a request will spend. A queue-depth
check cannot see that five panes are about to dispatch 120k-token prompts.

244's plan rejected wiring a ledger to an uncalibrated estimator and a guessed
scope, because that yields a *false* TPM guarantee: the number exists, looks
enforced, and is wrong. This spec is the contract that has to hold before an
enforcement flag may be turned on.

## Authority — who owns the budget

- One scope per lane: `(upstream, account, model)`. It comes from operator
  configuration, never from a request header, a caller-supplied cost estimate,
  or a bearer hash. `_bearer_id` hashes `Authorization` and collapses `x-api-key`
  to `api-key`, so it identifies a credential, not an account.
- Canonical model mapping: catalogue ids, aliases and vendor ids resolve to one
  budget key, or the request is *unknown-scope*.
- Unknown scope, model or missing cost bound: fail closed ONLY when strict mode
  is enabled. Otherwise the request proceeds unaccounted and the lane stays in
  observe mode, publishing metrics without refusing anything.
- Scope must be resolvable per dispatch, not per connection: account routing can
  change mid-session.

## Accounting — what is reserved, and when it is released

- **Reservation** = validated final input tokens (after body shrink and model
  selection) + an output bound (`max_tokens` when present, otherwise the model's
  default). Validated complete input is required; an unparsable body cannot be
  reserved for.
- **Weighting**: full weight for every token. No cache-discount assumption may
  inflate headroom, and no refund may be issued for an unknown provider discount
  — an unproven saving is not a saving.
- **Windows**: 60 s rolling request count (RPM bound) and 60 s rolling token sum
  (TPM bound). Plan/credit windows stay the plan meter's job; the ledger must not
  re-derive them.
- **Debt**: on 429, timeout or missing usage, the reservation is retained. Only a
  known-unspent reservation rolls back. Rolling back on timeout is how a ledger
  starts granting capacity it never got back.
- **Restart debt**: reservations that outlive a process restart are retained.
  There is no empty-on-restart full-budget shortcut — that is the single easiest
  way to silently double a fleet's spend.

## Atomicity and placement

- Check and debit happen with **no await between** them, per `(account, model)`.
- Placement is after final body/model/auth selection *and* after pacing, at every
  site that actually spends the provider's allocation — the 244 audit list:
  `_forward_once`, `_forward_once_into_sse` (keepalive hold), `_retry_direct_once`
  (including central fallback), `_probe_upstream_auth_once` and
  `_credential_recheck_one` (direct `session.post`, bypassing both forwarding
  seams), and `ingress.handler` (its own `session.request`). A single check in
  `_forward_once` is not coverage.
- Reuse the existing fair queue; do not add a second scheduler. The first
  integration may refuse locally with provenance rather than sleep while holding
  an inference slot.
- Internal probes are accounted to their own small budget so diagnostics can
  never starve the fleet.

## Refusal semantics

- A local refusal is `503` + `Retry-After` + a provenance header naming the
  budget and the reason. It is **never** fed into the provider AIMD or retry
  ladders: local admission backpressure is not vendor pushback, and treating it
  as such collapses a healthy ceiling.
- A denial that lands after an SSE response is committed has to use the terminal
  error channel; a truncated stream is not a refusal.
- Refusals are counted separately (`…_prospective_refusals_total{reason}`) so no
  dashboard confuses local policy with vendor limits.

## Rollout

1. Flag off ⇒ **byte parity** with today: no accounting on the response path, no
   new headers, no refusals.
2. Enable per lane, desktop MiMo first, with budgets set *above* the current
   measured peak so the ledger cannot bind during calibration.
3. Observe-only for one full day, then set the budgets from the observed p99.
4. Only then allow strict mode, and only for the narrowest lane.

## Falsifiers — each must fail before enforcement is on

1. default-off byte parity: the same request yields identical bytes and status;
2. concurrent overspend: N concurrent fat-context requests cannot exceed the
   token budget (244's 120-units-against-100 reproduction);
3. request-window debit: completed requests *and* retries debit the 60 s window;
4. cache-hit accounting: a cache-read request does not inflate headroom;
5. output variants: absent `max_tokens` resolves to the model default bound;
6. refusal provenance: a local 503 carries the header and does not move AIMD;
7. partial stream: a denial after commit reaches the client as a terminal error;
8. cancellation: before dispatch the reservation rolls back; after dispatch it is
   retained;
9. restart: debt survives a process restart;
10. topology: central fallback, internal probes and ingress are either accounted
    or explicitly refused — never silently unaccounted.

## Non-goals

No new dependency, no distributed ledger, no SDK, no change to `FairBearerLimiter`
fairness, no provider-account discovery, no guessing of the vendor's unpublished
concurrency ceiling, and no claim that a local budget can eliminate vendor 429s.
