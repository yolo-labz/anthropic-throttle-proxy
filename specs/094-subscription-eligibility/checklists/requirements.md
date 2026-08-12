# Specification Quality Checklist: Subscription Eligibility Enforcement and Attestation

**Purpose**: Validate specification completeness and quality before planning
**Created**: 12/08/2026
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] Focuses on caller and operator outcomes; exact wire details live in the frozen ADR-6a contract artifact
- [x] Explains why prevention and trusted receipts are both required
- [x] All mandatory sections are complete
- [x] Scope, compatibility, threat/failure model, and one-line reversal are explicit

## Requirement Completeness

- [x] No `[NEEDS CLARIFICATION]` markers remain
- [x] Requirements are falsifiable and unambiguous
- [x] Success criteria are measurable
- [x] All eligibility fixtures are enumerated (subscription eligible; direct-key both states ineligible; unknown ineligible)
- [x] Direct-key `detail="ok"` and proxy-held subscription near misses are distinguished
- [x] Unknown/unupgraded semantics fail closed
- [x] Session-pin and post-selection spill paths are covered
- [x] 403 policy refusal with two distinct reasons is specified
- [x] Per-request E3 freshness is specified
- [x] Health capability, cardinality, and latency bounds are specified
- [x] Unconstrained compatibility is specified
- [x] Dependencies and assumptions are identified

## Security and Governance

- [x] Positive eligibility uses the frozen E1/E2/E3 allowlist
- [x] Lane names, `detail` strings, and custody are explicitly forbidden classifiers
- [x] Upstream attestation spoofing is in the threat model
- [x] No raw credential or client-topology exposure is permitted
- [x] No provider call, production mutation, Nix edit, deployment, or direct-key foundation is in scope
- [x] Different-family final review and executable falsifiers are required by the workflow

## Feature Readiness

- [x] Each user story has an independent executable test
- [x] Every functional requirement maps to a success criterion or threat/failure case
- [x] Existing traffic has an explicit compatibility boundary
- [x] The specification is ready for verification without user clarification

## Notes

Validated against the frozen ADR-6a interface (`/tmp/fleet-foundry-adr6a-interface.md`),
PO S3's fixtures, QA A1/A3/F4/F5/NM-1/NM-8/K3, and the repository constitution.
Header names, status codes, and classification rules belong in the frozen
contract and `plan.md`.
