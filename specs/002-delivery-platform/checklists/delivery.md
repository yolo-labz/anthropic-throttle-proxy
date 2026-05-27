# Delivery Quality Checklist: Throttle Proxy Delivery Platform

**Purpose**: Unit tests for the throttle / fallback / build-and-test requirements — do they specify behavior precisely enough that two engineers would implement the same thing without further conversation?
**Created**: 2026-05-26
**Feature**: [spec.md](../spec.md)

## Requirement Completeness

- [ ] CHK001 Are forwarding requirements fully specified for both `THROTTLE_UPSTREAM`-direct and `THROTTLE_CENTRAL_URL`-routed paths? [Completeness, Spec §FR-001, §FR-002]
- [ ] CHK002 Are AIMD shrink and growth conditions both specified, including the floor, the cooldown gate, and the run-length threshold? [Completeness, Spec §FR-004]
- [ ] CHK003 Are the four pushback signal classes (429, 503, 529, `Retry-After`) each given an explicit, distinct behavior in requirements? [Completeness, Spec §FR-004, §FR-005, §FR-006]
- [ ] CHK004 Is `THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT` behavior specified for the exact combination `queue_mode=off` + central configured + same-host burst, including what happens above the cap? [Completeness, Spec §FR-011]
- [ ] CHK005 Are independent-test recipes for US2 fallback specified end-to-end (start healthy, lose central, observe `central_status=down`, recover) with no hidden steps? [Completeness, Spec §US2 Independent Test]

## Requirement Clarity

- [ ] CHK006 Is the AIMD multiplicative-decrease factor named with a concrete default rather than "shrink the ceiling"? [Clarity, Spec §FR-004]
- [ ] CHK007 Is "sustained 429/503 pushback" quantified (consecutive count, time window, or both)? [Clarity, Spec §FR-004]
- [ ] CHK008 Is "uncapped Retry-After" defined explicitly to mean the proxy honors header values larger than the AIMD cooldown? [Clarity, Spec §FR-006]
- [ ] CHK009 Is the unified-window `rejected` state distinguished from `allowed_warning` with reset-epoch behavior named for each? [Clarity, Spec §FR-007]
- [ ] CHK010 Is `THROTTLE_UTILIZATION_TARGET=0` documented as the disable sentinel rather than "low utilization"? [Clarity, Spec §FR-008]

## Requirement Consistency

- [ ] CHK011 Do the four queue modes (`off`, `observe`, `fair`, `reactive`) have consistent definitions across spec, plan, and tasks, with `reactive` always called out as an alias of `fair`? [Consistency, Spec §FR-004, Plan §Technical Context]
- [ ] CHK012 Are the per-bearer fair-queue and process-global dispatch-lock requirements internally consistent — one bounds concurrency, the other bounds rate? [Consistency, Spec §FR-009, §FR-010]
- [ ] CHK013 Does the central fallback requirement (FR-002) align with the central health interval and timeout knobs documented in the env-vars contract? [Consistency, Spec §FR-002, Contract §central knobs]
- [ ] CHK014 Are body-shrink env vars documented as a dropped feature in the env-vars contract so they do not contradict the spec's silence on body shrinking? [Consistency, Contract §body shrink]

## Acceptance Criteria Quality

- [ ] CHK015 Is "Zero hard rate-limit errors surface to the IDE during the storm" objectively verifiable from client-visible response codes alone? [Measurability, Spec §SC-001]
- [ ] CHK016 Is the "below 1% for new requests during the first health interval" target instrumented (which counter rolls up to a percentage)? [Measurability, Spec §SC-002]
- [ ] CHK017 Is the ≥~85% coverage target paired with the exact tool (SonarQube) and token convention (`PROJECT_ANALYSIS_TOKEN`, not `USER_TOKEN`)? [Measurability, Spec §SC-007, §FR-021]
- [ ] CHK018 Is the central up/down transition latency target ("within one health interval") tied to a concrete env var (`THROTTLE_CENTRAL_HEALTH_INTERVAL`) so a reviewer can time it? [Measurability, Spec §SC-008]

## Scenario Coverage

- [ ] CHK019 Are requirements present for the case where central health flaps (alternating up/down within one interval) rather than just steady-state up or down? [Coverage, Spec §Edge Cases]
- [ ] CHK020 Is the same-bearer-many-clients fairness scenario covered with a concrete failure mode (starvation) and a concrete remedy (round-robin across `client_id`)? [Coverage, Spec §FR-010, §Edge Cases]
- [ ] CHK021 Are scenarios where `Retry-After` arrives concurrently with a unified-window `rejected` covered (which deadline wins)? [Coverage, Spec §FR-006, §FR-007]

## Edge Case Coverage

- [ ] CHK022 Is the 529-not-shrink-cap rule paired with a separate counter requirement so operators can distinguish Anthropic capacity events from client throttle pressure? [Edge Case, Spec §FR-005]
- [ ] CHK023 Is the OAuth unified-window auto-pause behavior specified to release exactly at the reset epoch and not before, even if utilization drops? [Edge Case, Spec §FR-007]
- [ ] CHK024 Is the burst-pacing dispatch gap specified to apply across all bearers, not per bearer, with a process-global lock? [Edge Case, Spec §FR-009]

## Non-Functional Requirements

- [ ] CHK025 Is the streaming requirement (FR-003) quantified — what backpressure or memory bound counts as "without buffering the full body"? [Non-Functional, Spec §FR-003]
- [ ] CHK026 Is the Docker image requirement (FR-020) tied to verifiable signals (`FROM ... AS`, `uv sync`/`uv pip`, `hatchling`) rather than just "multi-stage"? [Non-Functional, Spec §FR-020]
- [ ] CHK027 Are docs (`CLAUDE.md`, `README.md`, `docs/DEPLOY-DOKKU.md`, three skills) named as a set required to agree, rather than each documented independently? [Non-Functional, Spec §FR-023]

## Dependencies & Assumptions

- [ ] CHK028 Is the assumption that clients honor `ANTHROPIC_BASE_URL` paired with a list of clients confirmed to do so (`claude-code`, `opencode`, `codex`, Anthropic SDKs)? [Assumption, Spec §Assumptions]
- [ ] CHK029 Is the single-worker central tier deployment named as an explicit out-of-scope boundary so a reviewer does not infer multi-worker support? [Assumption, Spec §Assumptions]
- [ ] CHK030 Is the GROQ-as-default-advisor assumption paired with the configuration-only switch claim (no code change to swap providers as long as they are OpenAI-compatible over raw aiohttp)? [Assumption, Spec §Assumptions, §FR-018]

## Ambiguities & Conflicts

- [ ] CHK031 Is "~85%" coverage the exact threshold, or is a more precise minimum needed for the CI gate? [Ambiguity, Spec §SC-007, §FR-021]
- [ ] CHK032 Is the quickstart's `https://anthropic-throttle.<your-host>` example reconcilable with the actual Tailscale-internal `http://` endpoint, or is there a documentation drift to resolve? [Ambiguity, quickstart.md vs deployed central]

## Notes

- Each item asks a question about the requirement itself, not the implementation. Items live alongside CI gates in `tasks.md` but they exist to catch ambiguity in the spec, not to certify the code.
- Use `[x]` to mark an item resolved (requirement updated or confirmed as written). Use comments inline when a finding closes via spec text rather than code change.
