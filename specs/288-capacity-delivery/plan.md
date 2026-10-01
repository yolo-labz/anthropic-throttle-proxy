# Recovered MiMo capacity delivery

## Scope and source

Integrate 279 (281 telemetry + 282 fairness contract) with landed client hotfix
9fc1eed and preserve worker 283/284/285 changes without editing their worktrees.
Workspace scope is side-projects:w1P. No increased provider concurrency, private
payload routing changes, new seat assignment, or purchases.

## Hypotheses and executable falsifiers

1. Static-pool retirement fails because the healthy-unconfigured OAuth escape
   also preserves retired static credentials; explicit credential death must
   override cached usage. The four recovered routing regressions fail before
   changes (two retirement cases, quarantined caller, unequal live limits).
2. Raw inflight counts concentrate arrivals on a reduced-capacity bearer;
   normalize weighted queue/occupancy pressure by its live capacity, preserving
   the existing score scale and retry/budget gates. Test both equal and unequal
   caps and exact handler completions, not just scoring.
3. Worst-wins provider joins incorrectly call MiMo exhausted when its individual
   plan is exhausted but Team has quota. Classify independent seats separately;
   mixed capacity is limited, not all exhausted. Invalid numbers are unknown;
   one binding exhausted window cannot be hidden by another window's headroom.
4. Team sampling must not depend on an informational historical counter, and
   the configured project path must be validated before any browser operation.
   Unassigned inventory belongs in display configuration, not invented quotas.

## Baseline evidence

- `pytest tests/test_proxy_helpers.py -k 'retired_incoming or symmetric_normalized or quarantined_incoming'`: 4 failed, 139 deselected.
- Recovered UI + Team/lanes suites: 92 passed (not sufficient to catch the mixed-seat truth defect).
- Recovered real-handler acceptance: 6 passed before normalization; verify its
  queue scenario still fills every alternative after load-aware routing changes.
- MiMo runtime active with 20 inflight and queued demand; no safe higher upstream
  concurrency ceiling established. Dashboard still runs old source.
- `gh auth token` times out before any network connection: login Secret Service
  collection locked. This is a local credential gate, not proven GitHub outage.

## Delivery

Run targeted and full pytest/ruff, client-native regressions, synthetic fairness,
and rendered fixture checks. Review mixed Chinese-frontier/OpenAI authorship
without treating model advice as CI. Publish one source PR with executable
acceptance; then Nix package + client pins and exact report validation. Runtime
activation is separate and must preserve inflight work and persistence/GC roots.

## Tasks

- [x] T01 Copy surviving worker edits into isolated 288; preserve originals.
- [x] T02 Merge 279 with current main (includes standalone admission fix).
- [x] T03 Reproduce routing failures and verify recovered baseline tests.
- [x] T04 Repair shared routing with explicit static/OAuth source type and normalized load.
- [x] T05 Correct capacity projection, Team parser boundaries and unassigned display.
- [x] T06 Run combined tests, lint, rendering and optional bounded review (unavailable, not approval).
- [ ] T07 Deliver source through PR and actual CI.
- [ ] T08 Complete Nix/client wiring and authorized live verification.
