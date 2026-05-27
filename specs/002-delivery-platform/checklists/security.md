# Security Requirements Checklist: Throttle Proxy Delivery Platform

**Purpose**: Unit tests for the bearer-identity, secret-hygiene, and lazy-import requirements that protect the proxy's secrets-at-the-boundary posture.
**Created**: 2026-05-26
**Feature**: [spec.md](../spec.md)

## Requirement Completeness

- [ ] CHK001 Are all four operator-visible surfaces (logs, metric labels, dashboard, health JSON) enumerated by name in the "no raw token" requirement, rather than left as "anywhere"? [Completeness, Spec §FR-015]
- [ ] CHK002 Is a bypass identity for unauthenticated probes specified, named (`_anon`), and reserved as a constant so probe traffic does not pollute per-bearer state? [Completeness, Spec §FR-016]
- [ ] CHK003 Are advisor secret-handling requirements specified for both the credential source (Bitwarden `api/groq` via `rbw`) and the runtime gate (`ADVISOR_ENABLED=true` AND `GROQ_API_KEY` present)? [Completeness, Spec §FR-018, §Assumptions]

## Requirement Clarity

- [ ] CHK004 Is the bearer ID derivation specified with the exact transformation (`sha256(Authorization-header)[:8]`) rather than "a hash"? [Clarity, Spec §FR-015]
- [ ] CHK005 Is "lazy import" defined to mean the advisor module is not imported until both gates pass at runtime, not just optional at install time? [Clarity, Spec §FR-019]
- [ ] CHK006 Is the GROQ payload constraint specified — the advisor's request body MUST NOT include raw bearer tokens — rather than implied by the broader no-tokens rule? [Clarity, Spec §SC-005]

## Requirement Consistency

- [ ] CHK007 Does the no-token rule apply consistently to GROQ advisor payloads (out-of-band call to an unrelated provider) the same way it applies to local logs? [Consistency, Spec §FR-015, §FR-018]
- [ ] CHK008 Is "no vendor SDK on the hot path" reconciled with the lazy-imported advisor module so the advisor's GROQ call (over raw `aiohttp`) is permitted? [Consistency, Spec §FR-018, §FR-019, Constitution Principle I]
- [ ] CHK009 Are the bypass slot (`_anon`) and bearer hash conventions aligned so probe traffic and authenticated traffic never share queue state? [Consistency, Spec §FR-015, §FR-016]

## Acceptance Criteria Quality

- [ ] CHK010 Is the no-raw-tokens success criterion verifiable by automated test (grep across logs, metrics, JSON, dashboard HTML) rather than manual inspection? [Measurability, Spec §SC-005]
- [ ] CHK011 Is the advisor-gate success criterion verifiable by attempting `POST /ui/advisor` with the gates off and asserting a non-2xx response, rather than only the positive path? [Measurability, Spec §FR-018]

## Scenario Coverage

- [ ] CHK012 Are scenarios specified where the advisor is enabled but `GROQ_API_KEY` is missing — what does the proxy do, and how does the operator notice? [Coverage, Spec §FR-018]
- [ ] CHK013 Are scenarios specified for the bearer-hash collision case (two raw tokens with the same 8-character prefix) and the limiter behavior under collision? [Coverage, Gap]
- [ ] CHK014 Is the case "operator copies `/__throttle/health` into a chat or paste bin" addressed — JSON must remain secret-free? [Coverage, Spec §SC-005]

## Edge Case Coverage

- [ ] CHK015 Are scenarios specified for the bearer header being malformed (missing, non-`Bearer`, empty) so the proxy does not log the offending header value? [Edge Case, Gap]
- [ ] CHK016 Is the advisor verdict text specified to never echo back tokens that may have appeared in throttle events (e.g., an upstream error body that includes a key)? [Edge Case, Spec §SC-005]

## Non-Functional Requirements

- [ ] CHK017 Are secret-handling responsibilities allocated — proxy never persists tokens; operator fetches via `rbw`; CI uses `PROJECT_ANALYSIS_TOKEN` not `USER_TOKEN`? [Non-Functional, Spec §FR-021, §Assumptions]
- [ ] CHK018 Is the dashboard's HTMX-without-modules constraint connected to a security rationale (one `<script>` source = small surface for token-stealing supply chain attacks)? [Non-Functional, Spec §FR-017]

## Dependencies & Assumptions

- [ ] CHK019 Is the `rbw`-first credential-fetch convention specified as the canonical operator workflow rather than left implicit? [Assumption, Spec §Assumptions]
- [ ] CHK020 Are the trust assumptions (loopback for local tier, HTTPS for central) made explicit so a reviewer knows the spec does not promise transport-level auth on `127.0.0.1:8765`? [Assumption, Spec §Assumptions]

## Ambiguities & Conflicts

- [ ] CHK021 Is the GROQ provider lock-in clarified — "OpenAI-compatible" means raw HTTP shape, not OpenAI's specific endpoints? [Ambiguity, Spec §Assumptions]

## Notes

- Items here exist to keep the constitution's Principle I (no vendor AI SDK on hot path) and Principle II (bearer hash never raw token) testable from the spec alone.
- A failed item is a spec edit, not a code edit — close the ambiguity in spec text before adjusting implementation.
