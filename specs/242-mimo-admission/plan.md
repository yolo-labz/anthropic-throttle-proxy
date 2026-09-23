# OpenAI generation shares bearer cooldown — 23/09/2026

## Hypothesis / observed failure

The shared admission cooldown excludes OpenAI-compatible generation paths, so a
sibling dispatches during the pause recorded for a preceding headerless 429.
Falsifier: a second `/v1/chat/completions` request must wait that recorded pause
on the old source. It did not: the HTTP regression observed 5 ms against 50 ms.

Live evidence from a dedicated instance: headerless `429` + `Too many requests`
recorded a 30-second synthetic pause; a new OpenAI handler started before it
expired. The same callers previously bypassed the proxy entirely. These are two
local admission defects, not proof of the vendor's unpublished capacity.

## Smallest fix

- Reuse `_retry_after_blocks_path` for Chat Completions and Responses as well as
  Messages, including vendor path prefixes; exclude usage/profile/models,
  count_tokens and response retrieval by id.
- Use the same selector when claiming half-open probes. Otherwise the new paths
  would wait on cold-start probation that they can never claim.
- Leave Anthropic-only credential routing, upstream choice and bodies untouched.
  Scope is per bearer, per proxy process; it is not cross-account admission.

## Executable acceptance

- Old source: 1 failed / 3 passed in the HTTP reproduction. The sole failure was
  `sibling bypassed shared cooldown` (5 ms < 45 ms lower tolerance).
- Fixed source: 16 targeted tests passed; full suite 1,337 passed; Ruff clean.
- Actual aiohttp paths verify an over-cap positive control, fair queue success,
  byte-identical SSE, bounded first-429 retry, shared sibling pause and no slot
  leaks. Cold-start probation can serialize the first off-mode call, so the
  control tests actual refusals rather than an incidental count.

## Advisory review disposition

Different-family GLM review was advisory, not approval. Existing safeguards
answer the conditional findings: handler task cancellation calls `finish_probe`
and the `finally` paths release it; probe gates exist separately from limiter
allocation; `_bearer_id` returns `_anon`, never an empty id; one isolated instance
and one account bearer define this rollout's sharing scope. Handler rechecks
retry-after before and after acquiring a slot. Suffix matching is intentional
for vendor-prefixed generation endpoints, not an assertion that every provider
uses `/v1`; telemetry negatives are tested. No new account routing is enabled.

## Delivery boundary

Deploy only to the dedicated MiMo instance. The shared package pin and other
inference processes stay unchanged. Preserve cooldown state across restart,
wait for zero inflight, verify effective/persisted/runtime package parity.
Synthetic success is NOT live vendor recovery. Bounded live checking still saw
429s; do not increase retries indefinitely, buy another plan, or call this a
proven vendor-capacity fix. Recover stalled tabs separately with privacy-eligible
models and observe fresh assistant receipts, not just changed footers.

Rollback: one revert PR of this squash commit; restore only the MiMo instance's
prior package if needed. Never restore uncontrolled direct fleet traffic merely
to make a local queue disappear.
