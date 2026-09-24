# Plan — bounded evidence and opt-in design

## Reuse assessment

Reuse `FairBearerLimiter` fairness/leases, existing deadline and shared cooldown
checks, `pacing._pace_dispatch`, aiohttp test servers and the process-local
metrics registry. Stdlib is sufficient for a future reservation ledger; no new
dependency or distributed service is justified for one admitting process.

Do not reuse `_expected_request_util_cost` as a token counter: it is a capped
Anthropic utilization heuristic, not an atomic debit. `_short_request_hint`
handles only `max_tokens`; `_record_usage` is called only for Messages, and
`_parse_sse_usage` ignores OpenAI `prompt_tokens`/`completion_tokens`. Completion
accounting is too late to prevent a concurrent prospective overspend anyway.

`_bearer_id` hashes Authorization but collapses x-api-key to `api-key`. Neither
establishes provider account/model ownership. Never accept a caller-supplied
account header or cost estimate as authoritative scope/accounting.

## All generation egress sites audited on 7ddf462

- `handler` → `_forward_with_retry` → `_try_forward` → `_forward_once`.
- `_retry_direct_once` also reaches `_try_forward`, including central fallback.
- `_keepalive_hold_and_retry` → `_forward_once_into_sse` has a separate outgoing
  request; it cannot be covered by changing only `_forward_once`.
- `_probe_upstream_auth_once` and `_credential_recheck_one` generate real token
  requests through direct `session.post`, bypassing both forwarding seams.
- `ingress.handler` remaps/spills/retries through its own `session.request`.
  It must target the authority; its admission GET snapshot is not a reservation.
- Account/central/UI/control GETs are not generations. The advisor's GROQ POST
  is a different provider and must not spend MiMo's allocation.

## Minimum future implementation, explicitly not shipped

One opt-in, dedicated direct-upstream account authority; all budgets unset/off
by default. Explicit operator-owned scope and canonical model mapping; unknown
scope/model/cost fail closed only when strict enforcement is enabled. Reject
unsupported central/probe configurations rather than silently bypassing them.

After final body/model/auth selection and pacing, atomically check/debit request
and token allocations at **every** actual provider dispatch (no await between
check/debit). Reuse the existing fair queue rather than add a second scheduler;
first integration may refuse locally with provenance instead of sleeping while
holding an inference slot. Do not feed local denials into provider AIMD/retries.
A denial after an SSE response is committed needs its terminal-error channel.

Validated complete input plus output/reasoning bound is required. Unknown
provider cache discounts never justify refunds. Retain dispatched debt on
429/timeout/missing usage; roll back only known-unspent reservations. Long
streams, cancellation and restart need explicit uncertainty and debt retention
semantics before enabling anything. No empty-on-restart full-budget shortcut.

## Decision

Deliver tests/spec only. A generic local ledger is implementable, but wiring an
uncalibrated estimator and guessed scope would produce a false TPM guarantee.
Shipping a disconnected unused ledger is unnecessary code. The prerequisite
contract and the implementation acceptance matrix are the next slice, not a
live limit change disguised as this investigation.

Further acceptance: input/tools/images, cache hit accounting, output variants,
invalid/unknown cost, shared vs distinct accounts, aliases, retries including
held SSE and direct fallback, internal probes, denied-admission atomicity,
cancellation before/after dispatch, long streams, restart debt, default-off
byte parity, topology bypass and deadline/counter cleanup. None is implied by
the six reproductions alone.

## Constitution and review

Principles I–V unchanged: no SDK, secret exposure, AIMD modification, live probe
or hard-coded production upstream. Generator: OpenAI. Pedro explicitly forbids
new subagents/Z.AI calls for this track; no automated review is requested and no
independent model approval is claimed. Executable acceptance is the evidence.
No deployment; one revert PR undoes this test/spec slice.
