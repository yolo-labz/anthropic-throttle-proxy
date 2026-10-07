# 314 — truthful static-analysis integration (07/10/2026)

Generator family: **openai**. Operator dispatch/contract: Throttler plan of
07/10/2026; existing canonical Sonar runbook and 06/10 metrics oracle.
No additional model delegation, runtime activation or shared-note edits.

## Specification and falsifiable hypothesis

Community Build exposes exactly one `main` branch, but the existing PR workflow
publishes PR revisions into it. Its unset-host path succeeds without scanning.
The report has no source/freshness/inventory validation before publication.
Restricting publication to main, requiring credentials/config there, and checking
fresh complete coverage against the clean checkout closes these attribution and
false-green gaps. Falsifiers: native PR branches exist, logs show isolated PR
analysis, or malformed/missing/stale/unmatched coverage still passes the checker.

Baseline at `3c64a535ea6902f00245173696d1005d5114eb0a`: Community
26.3.0.120487, single main branch, A/A/A, 0 issues, 0 hotspots, coverage 93.5%
(overall/new), duplication 0% (overall/new). Gate OK. Those are **src-only**
metrics, not all repository code; helpers and the Pi client are absent.

## Implementation plan

1. Keep required PR check `scan`, but make it a coverage/report preflight only.
   Publish to the shared Community project on main push/main dispatch only.
   Explicitly identify the analyzed revision and preserve the source/report/task
   receipts. Missing host/token must fail main publication, never skip green.
2. Expand first-party scan roots to src, scripts, clients, load, specs,
   .github and Dockerfile; extend Python coverage to helper/spec Python sources. Do not add exclusions,
   change server thresholds or conceal legacy findings. Preserve the earned
   80% floor; non-Python coverage remains a documented gap.
3. Add a stdlib report checker: clean exact Git revision, fresh non-empty XML,
   unambiguous tracked filenames, complete Python inventory and consistent line
   counts. Emit SHA-256 report/source hashes. Reject invalid reports rather than
   generating empty stand-ins. No network or credentials in the checker.
4. Run planted failures, full coverage, Ruff, types, slop/alignment and actual CI.
   Capture exact-head and baseline security/dependency receipts. Findings outside
   analysis-owned surfaces become explicit follow-ups, not source suppressions.

## Tasks / acceptance

- [x] Read canonical references; inspect live read-only Dokku/GitHub/Sonar.
- [x] Implement checker, red-capable tests and integration diff (23 tests pass).
- [ ] Obtain fresh complete-namespace coverage and exact-head PR checks.
- [x] Document inventory, scopes, measured baseline, findings and authorization.

Initial admitted full run passed 2,072 tests but its incomplete coverage inventory
was rejected. Native namespace discovery is enabled; stale dataset regeneration
is not acceptance. Heavy admission subsequently refused even with the unit dead;
no unit cleanup/restart is authorized. Use existing CI for the fresh rerun.
- [ ] Open ordinary PR. Actions changes require operator merge authorization;
      no self-merge, ruleset changes or production deploys in this workstream.

Constitution check: no proxy/runtime code or SDKs, bearer handling, health path,
limiter/pacing or service activation is changed. No score gaming or new dependency.
Three repair attempts / approximately 500 source lines maximum.

Rollback after authorized merge: revert its squash commit in one ordinary PR.
Community Sonar acceptance for the changed scope is necessarily **post-merge**;
PR-native checks cannot be represented as exact-head Community analysis.
