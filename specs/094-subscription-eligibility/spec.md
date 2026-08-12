# Feature Specification: Subscription Eligibility Enforcement and Attestation

**Feature Branch**: `094-subscription-eligibility`
**Created**: 12/08/2026
**Status**: Implementation
**Input**: Fleet Foundry K3 / ADR-6a frozen interface (`/tmp/fleet-foundry-adr6a-interface.md` §0–§6). Controlled workloads must use a positively proven subscription credential and never spill to direct/pay-go or unattestable capacity.

## User Scenarios & Testing

### User Story 1 — Constrained work cannot spend an ineligible credential (Priority: P1)

A trusted local caller can require subscription-backed execution for one request. The ingress selects only a positively eligible subscription route before any model egress. If no such route can serve the inferred role, the request is refused (403 policy verdict) rather than spilled to direct/pay-go or unknown capacity.

**Why this priority**: Prevention is the product boundary. Detecting an ineligible lane after its key was spent does not satisfy the subscription-only guarantee.

**Independent Test**: Exercise the eligibility fixtures against hermetic fake lanes and prove that eligible subscription lanes are called, every ineligible fixture is never called, and all-unavailable fixtures return a stable 403 refusal with a distinct reason and reset hint when one is known.

**Acceptance Scenarios**:

1. **Given** a request requiring the subscription contract and a lane whose fresh health proves CLASS (E1∧E2∧E4) ∧ CAPACITY (E3), **when** the request arrives, **then** that lane may be selected and the request succeeds with a `credential-mode: subscription` stamp.
2. **Given** an open direct/pay-go lane (with no bearers or with usable bearers and `detail="ok"`), **when** the requirement is sent, **then** the ingress refuses before that lane receives the request.
3. **Given** an unknown/unattestable lane, **when** the requirement is sent, **then** the ingress fails closed before model egress.
4. **Given** every eligible subscription lane is unavailable, **when** the requirement is sent, **then** the ingress returns a stable 403 `eligible_lanes_exhausted` and includes a reset hint when trusted lane facts provide one.
5. **Given** no lane is of the required class, **when** the requirement is sent, **then** the ingress returns 403 `no_eligible_lane` (distinguishable from exhausted).
6. **Given** a fail-on-call direct-provider trap beside the fake lanes, **when** every constrained fixture runs, **then** the trap records zero calls.

---

### User Story 2 — A successful response proves its credential class (Priority: P1)

A caller receives trusted lane, role, and credential-mode evidence on every successful response. Missing, unknown, or lane-spoofed credential evidence cannot be accepted as subscription-backed execution.

**Why this priority**: Enforcement without a receipt is a promise the caller cannot verify. The attestation must be authored by the ingress that made the selection, not copied from an untrusted upstream response.

**Independent Test**: Return a spoofed credential stamp from a fake lane and verify the ingress strips it; verify a successful response carries the canonical stamp matching the lane's actual class, and `unknown` carries a stable reason.

**Acceptance Scenarios**:

1. **Given** an eligible route returns a success, **when** the request used the subscription requirement, **then** the response carries exact `credential-mode: subscription` beside the existing lane and role evidence.
2. **Given** a lane returns its own credential stamp, **when** the ingress relays the response, **then** the lane-authored stamp is removed and only an ingress-authored stamp may appear.
3. **Given** a lane cannot be classified, **when** the ingress relays the response, **then** `credential-mode: unknown` plus a stable `-reason` is emitted — never an absent field.
4. **Given** an unconstrained request, **when** a response is relayed, **then** it still carries the actual `credential-mode` of the serving lane (with `-reason` when unknown).

---

### User Story 3 — Upgrade and availability state are fail-closed and observable (Priority: P2)

A caller can discover whether an ingress supports the enforcement contract before sending a model request. Operators can inspect a compact, O(lanes) per-lane view of credential mode and reason without exposing per-client maps.

**Why this priority**: An old ingress would otherwise ignore an unknown request header. Mandatory capability discovery prevents an unupgraded instance from spending a disallowed credential, and compact health keeps the existing sub-50 ms probe invariant.

**Independent Test**: Assert the upgraded health contract advertises `enforcement.credential_mode=true` + `contract="adr6a-credential-mode/1"`, absence of that advertisement is a caller-side refusal, unknown requirement versions are rejected without a lane call, and ingress health remains bounded and fast while source lane fixtures contain many bearer records.

**Acceptance Scenarios**:

1. **Given** an upgraded ingress, **when** a caller reads health, **then** it finds the explicit enforcement capability object.
2. **Given** an unupgraded or unknown contract advertisement, **when** a caller requires subscription execution, **then** it refuses locally and sends no model request.
3. **Given** an unsupported, malformed, or duplicated requirement on an upgraded ingress, **when** the request arrives, **then** the ingress rejects it before lane selection and model egress.
4. **Given** many per-bearer records in lane health, **when** ingress health is read, **then** it exposes only O(lanes) credential facts and no bearer/client topology.

### Edge Cases

- A session was pinned to a direct-key lane by earlier unconstrained traffic.
- The selected subscription lane saturates or returns a spillable pushback after admission.
- A lane reports `detail="ok"` while `api_key.enabled=true`.
- A lane reports only part of the credential evidence or malformed types.
- A lane attempts to spoof the credential-mode or -reason response headers.
- A reset is absent, elapsed, malformed, or differs across multiple unavailable bearers.
- `INGRESS_SUBSCRIPTION_UPSTREAMS` is unset or empty.
- Existing callers omit the new requirement entirely.

## Requirements

### Functional Requirements

- **FR-001**: The ingress MUST accept one versioned, request-scoped credential requirement whose mode is `subscription`.
- **FR-002**: Any requirement that is duplicated, malformed, unknown, or unsupported MUST be rejected before lane selection and model egress.
- **FR-003**: A caller requiring subscription MUST first verify the ingress health capability advertisement; absence or an unknown advertisement MUST be treated as unsupported and no model request may be sent.
- **FR-004**: Subscription eligibility MUST be a positive conjunction of E1 (`api_key.enabled=false`), E2 (upstream host ∈ `INGRESS_SUBSCRIPTION_UPSTREAMS`), and E3 (≥1 usable bearer carrying 5h/7d windows).
- **FR-005**: Eligibility MUST NOT be inferred from lane `detail` strings, lane names, or credential custody alone.
- **FR-006**: A direct/pay-go API-key lane MUST be ineligible whether it has no bearers or is healthy with usable bearers and `detail="ok"`.
- **FR-007**: Unknown, incomplete, or unattestable credential facts MUST be ineligible (`unknown` mode with a stable reason).
- **FR-008**: `INGRESS_SUBSCRIPTION_UPSTREAMS` unset or empty MUST mean no lane is eligible (fail-closed policy).
- **FR-009**: The requirement MUST constrain every pre-egress selection path, including session pins, initial selection, saturation spill, and retry re-selection, without overwriting shared unconstrained session pins.
- **FR-010**: When no eligible subscription lane can serve the inferred role, the ingress MUST return a stable 403 refusal and MUST NOT call an ineligible route.
- **FR-011**: The 403 refusal MUST distinguish `no_eligible_lane` from `eligible_lanes_exhausted` (with `reset_hint_epoch` when known) and MUST NOT enter capacity/AIMD/retry channels.
- **FR-012**: Every successful constrained response MUST carry ingress-authored `credential_mode=subscription` plus lane and role evidence.
- **FR-013**: Every CONSTRAINED 2xx response MUST carry the ingress-authored `credential_mode=subscription` stamp; the 403 refusal carries `unknown` (r1/C3 — the response enum collapses to these two by construction; the full four-value vocabulary is normative for per-lane health). Credential attestation headers received from a lane or upstream MUST be stripped before relay.
- **FR-014**: Ingress health MUST advertise the enforcement capability (versioned contract + allowlist count/digest) and expose O(lanes) per-lane credential mode + reason.
- **FR-015**: Ingress health MUST remain local-only, perform no request-path upstream I/O, expose no bearer or client map, and stay bounded/fast.
- **FR-016**: Requests that omit the credential requirement MUST retain existing routing, spill, pinning, and response behavior with NO credential-mode stamp on the response (r1/C3 — constrained-only stamping; unconstrained traffic is behaviorally unchanged) except that newly reserved credential headers cannot be relayed from an upstream.
- **FR-017**: Raw credentials, bearer values, API keys, and client topology MUST NOT appear in the new health, error, logging, or attestation surfaces.
- **FR-018**: The implementation MUST add no vendor SDK, provider dependency, deployment configuration, Nix change, or production mutation.

### Key Entities

- **Credential requirement**: A request-scoped, versioned precondition naming the required credential mode.
- **Credential mode**: The positive economic class of the serving lane (`subscription` / `direct_key` / `proxy_key` / `unknown`), authored by the ingress.
- **Eligibility refusal**: Stable 403 fail-closed response emitted before ineligible model egress, distinguishing no-eligible-lane from exhausted.

## Threat and Failure Model

- **Header spoofing**: A lane, provider, or client may return/send reserved credential headers. The ingress is the sole attestation authority and removes those values before relaying.
- **Silent downgrade**: A constrained request may encounter a session pin, spill, or retry path that was selected under unconstrained policy. Every selection path must reapply the requirement.
- **String/custody confusion**: `detail="ok"` and proxy-held custody occur on both sides of the economic boundary. Neither may classify eligibility.
- **Stale or incomplete evidence**: Missing or malformed credential facts are unknown, never subscription by default. E3 is re-verified per request.
- **Unupgraded ingress**: Old binaries cannot interpret a new header. The caller contract therefore requires capability discovery before the model request; an upgraded ingress independently rejects unknown request semantics.
- **Health growth**: Per-bearer source facts may be large. The ingress stores and emits one bounded summary per configured lane and never copies bearer/client collections into ingress health.
- **All eligible capacity unavailable**: Work blocks with a stable 403 reason and optional reset hint. There is no automatic direct-key fallback.

## Compatibility

- The new requirement is opt-in per request. Header absence preserves existing traffic.
- Existing role inference, model remapping, unconstrained lane order, account routing, queue handling, and provider behavior remain unchanged.
- The contract supports only `subscription`; future modes require a new advertised contract version rather than silently changing semantics.
- Existing lane health schemas remain accepted for ordinary availability. Only positively complete subscription facts qualify for constrained routing.

## Success Criteria

### Measurable Outcomes

- **SC-001**: All eligibility fixtures pass (subscription = eligible; direct-key with no bearers and with `detail="ok"` = ineligible; unknown/unattestable = ineligible), with zero direct-provider trap calls.
- **SC-002**: Successful constrained responses contain the exact canonical `credential-mode: subscription` stamp; spoofed or missing stamps fail; `unknown` always carries a reason.
- **SC-003**: Unknown, malformed, duplicated, or unsupported requirements produce zero lane calls and a stable client error.
- **SC-004**: A pre-existing pin to an ineligible lane and a post-selection saturation spill both remain unable to reach that lane under the subscription requirement.
- **SC-005**: Refusal is 403 (never 503), pre-egress, with distinct `no_eligible_lane` / `eligible_lanes_exhausted` reasons; an eligible-but-capped lane yields `eligible_lanes_exhausted` with a reset hint when known.
- **SC-006**: Existing unconstrained ingress tests and the full project test suite pass without altered expectations.
- **SC-007**: Deliberately neutering pre-selection enforcement, the response stamp, or spoof stripping makes the corresponding executable acceptance test fail.

## Assumptions

- The ingress and configured loopback lane processes share the trusted desktop boundary.
- For v1, a subscription credential CLASS is positively evidenced by E1∧E2∧E4 per the frozen ADR-6a r1 interface; CAPACITY (E3) is verified fresh per request.
- The caller is trusted local Fleet Foundry adapter code and follows mandatory health capability discovery before issuing a constrained model request.
- Signing is outside v1 under the trusted-worker threat model; transport trust terminates at the local ingress.

## Scope Boundaries

- No Bifrost, vendor SDK, Dokku credential, provider key, new service, production configuration, live model canary, Nix edit, or deployment.
- No permanent eligibility decision for currently unattestable sidecars; they fail closed until a future contract can positively attest them.
- The change is scoped to the proxy repo's ingress; the Fleet Foundry consumer/stub half is a separate S3 slice in `fleet-coordination`.
- The merge decision remains with the Throttler repo owner (Pedro).

## Reversal

One-line revert: `git revert <094-squash-sha>`.
