# Operations Requirements Checklist: Throttle Proxy Delivery Platform

**Purpose**: Unit tests for the probe-surface, observability, dashboard, and systemd-persistence requirements that operators rely on at 4 AM.
**Created**: 2026-05-26
**Feature**: [spec.md](../spec.md)

## Requirement Completeness

- [ ] CHK001 Are all four probe surfaces (`GET /`, `HEAD /`, `/__throttle/health`, `/metrics`) named and required to be locally answered with a 200, rather than left as "probe traffic"? [Completeness, Spec §FR-012, §SC-003]
- [ ] CHK002 Are the required top-level health JSON fields enumerated (`queue_mode`, `inflight`, `queued`, `served`, `central_status`, per-bearer `queued_per_client`) rather than left as "useful state"? [Completeness, Spec §FR-013, Contract §health-json]
- [ ] CHK003 Are the persistence verification commands specified by exact form (`systemctl --user cat`, `systemctl --user show -p ExecStart --value`) rather than "operators verify the unit"? [Completeness, Spec §FR-024]
- [ ] CHK004 Are the niri-guard deferred-activation symptoms and the surgical-symlink-swap remedy specified as a reproducible procedure rather than incident folklore? [Completeness, Spec §FR-025]
- [ ] CHK005 Are docs required to agree on invariants and procedures across `CLAUDE.md`, `README.md`, `docs/DEPLOY-DOKKU.md`, and the three skills (`throttle-incident`, `nix-user-service`, `deploy-dokku`)? [Completeness, Spec §FR-023]

## Requirement Clarity

- [ ] CHK006 Is "within 50 ms" specified as a p99 target under load, not just a typical-case latency? [Clarity, Spec §FR-013, §SC-004]
- [ ] CHK007 Is "process-local collector registry (not the global default)" defined precisely enough that a reviewer can grep for the right symbol (`CollectorRegistry()`)? [Clarity, Spec §FR-014]
- [ ] CHK008 Is "HTMX 1.x dashboard without JavaScript modules" defined to mean no Alpine, no React, no ESM imports, with a single `<script>` for HTMX itself? [Clarity, Spec §FR-017]
- [ ] CHK009 Is "Catppuccin Mocha palette tokens only" defined to mean no raw hex outside the tokens file, with a concrete tokens file path? [Clarity, Spec §FR-017]
- [ ] CHK010 Is the Dokku healthcheck interval (5 s) and timeout (5 s) named alongside the 50 ms p99 target so the operator can reason about restart policy interaction? [Clarity, Spec §FR-013, Contract §health-json]

## Requirement Consistency

- [ ] CHK011 Do the persistence verification requirements (FR-024) align with the surgical fix requirements (FR-025) — one detects, the other remedies, both no-reboot? [Consistency, Spec §FR-024, §FR-025]
- [ ] CHK012 Does the probe-traffic locality rule (FR-012) align with the bypass slot rule (FR-016) so probes share `_anon` and never consume per-bearer state? [Consistency, Spec §FR-012, §FR-016]
- [ ] CHK013 Are advisor surfaces (`/ui` rendering and `state["last_advisor"]`) consistent in what they expose — same verdict text, same timestamps, same gating? [Consistency, Spec §FR-018, Contract §last_advisor]

## Acceptance Criteria Quality

- [ ] CHK014 Is the 100% probe locality target measurable from a single log/grep — every `GET /`, `HEAD /`, `/__throttle/health`, and `/metrics` response without an upstream call? [Measurability, Spec §SC-003]
- [ ] CHK015 Is the post-reboot persistence success target verifiable from a single command (`systemctl --user show ... -p ExecStart --value`) compared against the expected store path? [Measurability, Spec §SC-006]
- [ ] CHK016 Is the central transition latency measurable in both directions (up→down and down→up) with the same `THROTTLE_CENTRAL_HEALTH_INTERVAL` bound? [Measurability, Spec §SC-008]

## Scenario Coverage

- [ ] CHK017 Are scenarios specified for the case where the desktop host has a runtime drop-in (`/run/user/<uid>/...`) pinning the new binary while the persistent symlink still points at the old one? [Coverage, Spec §US4 Acceptance Scenario 2]
- [ ] CHK018 Are scenarios specified for the case where the Dokku healthcheck returns 200 but the central proxy is failing to forward (false-healthy)? [Coverage, Gap]
- [ ] CHK019 Are scenarios specified for the dashboard under partial load (some bearers active, some idle) so the operator can confirm queue depths render correctly? [Coverage, Spec §US3 Independent Test]

## Edge Case Coverage

- [ ] CHK020 Is the case where `/__throttle/health` itself takes longer than 50 ms (e.g., event-loop blocker, lock contention) called out with a remediation hint (`py-spy dump`)? [Edge Case, Spec §FR-013, quickstart.md §What to do]
- [ ] CHK021 Is the case where the advisor has never run (`state["last_advisor"] is None`) covered so the dashboard does not render `null` as a literal string? [Edge Case, Contract §last_advisor]
- [ ] CHK022 Is the case where Home Manager activation is deferred indefinitely (reboot never happens) covered so the surgical fix remains stable across multiple deferrals? [Edge Case, Spec §FR-025]

## Non-Functional Requirements

- [ ] CHK023 Is the dashboard accessibility envelope specified — evergreen browsers only, no IE — so reviewers do not request broader compatibility? [Non-Functional, Spec §Assumptions]
- [ ] CHK024 Is the Prometheus scrape interval named anywhere, or is the 50 ms p99 target meant to bound any scraper without an interval contract? [Non-Functional, Spec §FR-013, §FR-014]

## Dependencies & Assumptions

- [ ] CHK025 Is the `nh os switch` → `nh os boot` rewrite (niri-guard) named as an assumption rather than a hidden quirk that a non-Pedro operator would have to discover? [Assumption, Spec §Assumptions, §FR-025]
- [ ] CHK026 Is the single-worker assumption restated for the dashboard's `state` dict so a reader does not assume multi-worker shared state? [Assumption, Spec §Assumptions]

## Ambiguities & Conflicts

- [ ] CHK027 Is the SonarQube ~85% threshold operationally bounded — does the CI gate fail at exactly 85.0%, or at some looser threshold? [Ambiguity, Spec §SC-007, §FR-021]
- [ ] CHK028 Does the quickstart example URL (`https://anthropic-throttle.<your-host>`) match the actual deployed central host (Tailscale-internal `http://`)? [Ambiguity, quickstart.md vs deployed central]

## Notes

- These items exist so that a fresh operator (or sibling Claude session) can take the dashboard, the health endpoint, and the persistence checklist as written and reach the same operational understanding without prior incident memory.
- A failed item should be closed by editing the spec or the relevant doc, not by changing runtime behavior.
