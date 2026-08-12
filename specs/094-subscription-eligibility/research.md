# Research: Subscription Eligibility Enforcement and Attestation

**Date**: 12/08/2026

## Decision 1: Enforce at `:8760`, not in Fleet Foundry

**Decision**: The existing ingress owns both pre-egress filtering and the
response stamp.

**Rationale**: It is the only component that chooses a lane. An adapter can
request a constraint but cannot prevent the ingress's role chain, session pin,
or spill path from choosing another lane (ADR-6a §1.1).

## Decision 2: Use a strict request token plus mandatory capability discovery

**Decision**: Canonical request value is `x-anthropic-throttle-require-credential-mode: subscription`;
ingress health advertises the enforcement capability and callers must discover
it before a model call.

**Rationale**: A strict static token has no ambiguous parser surface. A new
header alone cannot protect against an old ingress because old code forwards
unknown headers. Preflight discovery is therefore load-bearing, not optional.

## Decision 3: Classify by the frozen E1/E2/E3 allowlist, not `detail` or custody

**Decision**: A v1 subscription lane requires `api_key.enabled=false` (E1), an
upstream host in the explicit `INGRESS_SUBSCRIPTION_UPSTREAMS` config allowlist
(E2), and ≥1 usable bearer carrying 5h/7d window state (E3). Custody, lane id,
and availability `detail` are excluded. An empty allowlist means no lane is
eligible (absence of policy is not permission).

**Rationale**: Live `anthropic` and `deepseek` lanes both report `detail=ok` and
both are proxy-held, while only the former is subscription-backed. The frozen
ADR-6a interface (re-measured 12/08/2026 11:47) is the authoritative source.

## Decision 4: Per-request E3 freshness

**Decision**: Credential class (E1/E2) may come from the poll cache; usability
(E3) is re-verified by a fresh health probe of the candidate lane at constrained
selection time.

**Rationale**: A bearer can become unusable between polls. The attestation is a
claim about the request served; a stamp derived from a stale row asserts
something the request did not verify (ADR-6a §1.3, AC-21 defect class).

## Decision 5: Policy refusal is 403, never 503

**Decision**: No-eligible-lane refusals use `403` with a
`x-anthropic-throttle-refusal` header and two distinct reasons
(`no_eligible_lane` / `eligible_lanes_exhausted`).

**Rationale**: `503` in this codebase means capacity pushback — it feeds
`_should_retry_pushback`, AIMD shrink, and client-side retry. An eligibility
refusal is a policy verdict; retrying walks into the same wall and would shrink
a healthy lane for a reason unrelated to load.

## Decision 6: Stamp every response with the actual class

**Decision**: Every forwarded response carries
`x-anthropic-throttle-credential-mode: subscription|direct_key|proxy_key|unknown`;
`unknown` always pairs with a stable `-reason` token. Reserved headers from
client/upstream are stripped.

**Rationale**: `unknown` and absent are different operator answers (fix the
adapter vs accept the classification); neither may be representable as silence.
The ingress is the sole attestation authority.

## Decision 7: Keep constrained failures request-local

**Decision**: Exclude a failed lane from that constrained request without
changing shared `LaneState` for transport errors or bare 429/503 responses;
constrained selection never overwrites shared unconstrained session pins.

**Rationale**: A broadened constrained failure is not a health verdict. Closing
the lane globally would poison later unconstrained role routing.

## Decision 8: One atomic code/docs PR

**Decision**: Land the numbered spec, contract, tests, implementation, and
related architecture/handoff note together. One service, one-revert undoable.

## Decision 9: Spec number 094

**Decision**: Use Spec 094. `specs/` ends at 093; no 094 namespace exists.

## Decision 10: No new dependency

**Decision**: Use `urllib.parse`, stdlib, and existing dataclasses.

**Rationale**: The standard library covers URL host parsing and scalar
classification. A schema or SDK dependency would widen the hot path without
adding correctness.

## Reconciliation note (12/08/2026)

The initially implemented Spec 094 used a different wire shape (request
`credential-requirement: v1;mode=subscription`, `credential_contract` health
object, `503` refusal, location/custody/account-routing/staleness gates). The
frozen ADR-6a interface superseded it; the implementation, tests, and docs were
reconciled to the frozen wire contract (header names, `enforcement` capability,
`403` policy refusal, E1/E2/E3 predicate, per-request E3 freshness, actual-mode
stamping on every response).
