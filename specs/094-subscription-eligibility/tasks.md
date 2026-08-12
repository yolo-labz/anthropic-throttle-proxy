# Tasks: Subscription Eligibility Enforcement and Attestation

**Input**: [spec.md](spec.md), [plan.md](plan.md), [contract](contracts/credential-eligibility-v1.md), frozen ADR-6a interface
**Strategy**: One atomic K3 slice; tests first; no live model/deployment/Nix work

## Phase 1: Contract and tests

- [x] T001 [P] [US1] Add credential classification (E1/E2/E3), direct-key near-miss, allowlist fail-closed, and refusal-distinction unit tests in `tests/test_ingress_adr6a.py`
- [x] T002 [US1] Add eligibility fixtures, direct-provider trap, session-pin, spill, unknown-contract, and 403-refusal tests in `tests/test_ingress_adr6a.py`
- [x] T003 [US2] Add actual-mode response stamp, unknown-reason, and upstream-spoof rejection tests in `tests/test_ingress_adr6a.py`
- [x] T004 [US3] Add enforcement capability discovery + per-lane credential-mode health tests in `tests/test_ingress_adr6a.py`

## Phase 2: Implementation

- [x] T005 [US1] Extend `LaneState` with frozen credential fields in `src/anthropic_throttle_proxy/routing.py`
- [x] T006 [US1] Add `_classify_lane_mode` (E1/E2/E3), `_policy_refusal` (403), fresh-probe, and per-selection-path enforcement in `src/anthropic_throttle_proxy/ingress.py`
- [x] T007 [US2] Strip reserved credential headers from client/upstream; stamp `subscription` on constrained 2xx only (r1/C3) in `src/anthropic_throttle_proxy/ingress.py`
- [x] T008 [US3] Advertise `enforcement` capability + per-lane credential mode/reason in ingress health

## Phase 3: Documentation and external gates

- [x] T009 [P] Document the ADR-6a contract and compatibility boundary in `docs/ARCHITECTURE.md`
- [x] T010 [P] Record the K3 implementation/evidence boundary in `handoff.md`
- [ ] T011 Run `specs/094-subscription-eligibility/verify.sh` and the three enforcement/stamp/spoof mutation falsifiers
- [ ] T012 Obtain and resolve a different-family final adversarial review against the final diff, tests, evidence, and no-deploy plan
- [ ] T013 Push, open the PR, poll all required checks green, squash-merge, confirm `state=MERGED`, and record the one-line revert
- [ ] T014 Write `/tmp/fleet-foundry-k3-throttler-report.md`, then notify `fleet-orch`, `fleet-arch`, `fleet-po`, `fleet-qa`, and `fleet-eng`

## Dependencies and execution order

1. T001–T004 define the failing contract before implementation.
2. T005 precedes T006–T008; T006–T008 may share `ingress.py` and therefore run sequentially.
3. T009/T010 may proceed after behavior stabilizes and touch separate files.
4. T011 gates review; T012 gates PR merge; T013 gates the report; T014 is last.

## Independent story checks

- **US1**: eligibility fixtures + trap zero + stable 403 refusal with distinct reasons.
- **US2**: exact ingress-authored stamps; spoof/missing stamps fail; unknown carries reason.
- **US3**: enforcement capability discovery; unsupported requirements fail pre-egress; bounded health.

## Reversal

`git revert <094-squash-sha>`
