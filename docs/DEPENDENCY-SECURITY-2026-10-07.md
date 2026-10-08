# Dependency/security shipping repair — 07/10/2026

Generator family: **openai**. Isolated branch `332-dependency-security` from
`859ba6c62d3846d0ee3751b5368351f27c58c84d`. Branch314 and gated Actions PR315
are preserved; no Actions/CODEOWNERS/threshold/exclusion/suppression changes.
Receipts: [`security-332/`](security-332/); acceptance plan: `specs/332-dependency-security/plan.md`.

## Verified advisory, shipped lock, and actual failing behavior

Canonical [GHSA-54p9-h82j-f925](https://github.com/advisories/GHSA-54p9-h82j-f925)
and [OSV](https://api.osv.dev/v1/vulns/GHSA-54p9-h82j-f925) agree:
**CVE-2026-104874**, multidict **6.7.0 through 6.9.0 affected**, **6.9.1 first fixed**.
Root cause is unreleased identity/value references in C-extension items-view
reflected union and subtraction. CVSS 5.3/moderate; not a demonstrated remote
exploit against this proxy. The repository's own callers use ordinary iteration,
not these set operators; reachability through downstream callers is not asserted.
No dismissal or affected-range reinterpretation was used.

The shipped frozen `uv.lock` contains **6.7.1**. `uv sync --frozen --group dev`
installed 6.7.1 with **`multidict._multidict`**, not a silently safe pure-Python
implementation. The new regression ran against that installation **before**
repair: all four combinations (`CIMultiDict`/`MultiDict` × reflected union/
subtraction) retained exactly **one** operand-value reference (4 vs 3); lock-floor
and mutable-image tests also failed. **6 failed**, exit 1 (`red-before.txt`).
One operand and one operation per case; no stress, RSS growth loop or GC manipulation.

## Smallest real remediation and next concrete defect

- `uv lock --upgrade-package 'multidict==6.9.1'` with existing **uv 0.9.2**.
  Machine comparison of the old/new package maps proves **multidict is the only
  changed package version**. No new direct dependency or blanket update.
  Hashes/canonical metadata and the exact lock transition are retained.
- Fix the first concrete source-only remaining finding: mutable Docker image
  tags (#39/#40). Preserve the same Python 3.13-slim and uv 0.9.2 versions,
  but pin both Python stages, the uv copy image and Dockerfile 1.7 frontend to
  their currently resolved **multi-platform manifest digests**. HTTPS registry
  response digests match the SHA-256 of the actual manifest bytes. This makes
  those inputs immutable; it does not claim apt repositories or the entire
  build are byte-reproducible, signed, or free of every container CVE.
- One small regression module checks actual leaked references, the shipped lock
  is outside this advisory's range, and external Docker image inputs are pinned.
  No scanner findings are hidden. Existing immutable-action patterns are reused;
  no workflow changes or instance/repository permission changes.

## Ranked remaining findings (baseline IDs, not blanket dispositions)

| Rank | Findings | Evidence and action |
|---|---|---|
| 1 | OSV **#55** multidict / CVE-2026-104874 | Concrete affected installed C extension, failing reproduction; update first-fixed 6.9.1 here. Require exact-SHA OSV clearance, not its report-only green badge. |
| 2 | Scorecard **#39/#40** PinnedDependenciesID | Dockerfile mutable Python images; source defect repaired with current registry digests and executable guard. Also pin uv/frontend rather than leave sibling input mutable. |
| 3 | CodeQL **#13** weak-sensitive-data-hashing, `ratelimit.py:55` | SHA-256 is an anonymous bearer correlation/limiter ID, not password storage or authentication. Its format is a documented load-bearing invariant shared by accounts/proxy. Do not replace it with a password KDF to satisfy a query. Review-specific disposition remains open; no global suppression. |
| 4 | Scorecard **#46** SecurityPolicyID | Missing security policy is real disclosure/governance debt. Establish a verified reporting channel before adding copy that invents one; no unauthorized external messages/settings change. |
| 5 | Scorecard **#45** FuzzingID | Robustness improvement, not proof of a source vulnerability. Separate bounded fuzz/property slice; no host stress or empty fuzz badge. |
| 6 | CodeQL **#53** same rule, `tests/test_accounts.py:27` | Synthetic token fingerprint setup matching the runtime invariant; not evidence of stored password hashing. Leave open pending rule-specific review, without renaming data or suppressing the query to game results. |
| 7 | Scorecard **#38** BranchProtectionID | Live ruleset already enforces PR, required checks, resolved threads, no force-push/deletion; classic protection endpoint is not the ruleset surface. Additional score demands must be compared to actual rules; instance/repo governance changes not in this source slice. |
| 8 | Scorecard **#42** CodeReviewID | Human-review/history governance, not a source algorithm defect. Required approvals are currently zero; do not fabricate reviewer approval or change policy. |
| 9 | Scorecard **#44** CIIBestPracticesID | External badge/self-assessment, lowest executable-security signal. No fabricated attestation or enrollment. |

These are the requested **two CodeQL/seven Scorecard** findings, plus the
confirmed dependency alert. Security perfection is not claimed.

## Acceptance / publication boundary

Baseline red command: `uv run pytest -q tests/test_dependency_security.py`
after frozen installation. Source Ruff lint/format passed. Heavy green/full-test
admission was refused (first host-headroom budget, then stale loaded job-unit);
no fallback to heavy tests/builds in the small lane, global cleanup or unit restart.
Only the bounded 472ms lock metadata resolution ran in the admitted control lane.
Use existing GitHub CI for actual patched-package/full-test/build/security scans.
Pending receipts must remain pending until executed; a green report-only job is
not a clean vulnerability/type report.

### Actual patched-package acceptance

PR [#329](https://github.com/yolo-labz/anthropic-throttle-proxy/pull/329), source
head `da26d07b909b08f007264be20cc3e603ccc767bb`: all checks passed. The
coverage job explicitly installed **multidict 6.9.1**, ran the new module's
**six tests successfully**, and passed **2,146 total tests** (290 warnings,
98.99s). Actual CodeQL and OSV analyses at GitHub merge checkout
`e79cd22d65bb5d5c8d9c03e11c7f77be5ab20a41` each report **0 results**;
that PR's open-alert list is empty. The throwaway Docker build passed using
the pinned digests. Slop/alignment passed through normal hooks and CI.
Raw exact-revision run/security/build receipts are in `security-332/pr-*`.
This is real green-after acceptance, not a report-only job's status alone.
Existing main CodeQL/Scorecard findings remain separate, without dismissals.

Evidence-only follow-up commits require their own current-head checks before
merge. Post-merge main OSV/Scorecard results must be read before claiming the
historical alert #55 or image findings globally cleared; the held PR315 can
still contain its old lock. Runtime activation remains unperformed.

Delivery target: normal protected safe-class PR + squash merge, all required
checks/review threads satisfied; no Actions edits, no admin merge, force-push or
hook bypass. Actual deployment/activation is not performed. Rollback after merge:
one normal PR reverting the squash commit. PR315 remains a separate genuinely
gated Actions change; its expanded-scope main Sonar acceptance is not supplied
by this dependency/source slice. Canonical SAVE-STATE will cite verified merged
SHA and current-head receipts; user/sibling main/vault WIP remains untouched.
