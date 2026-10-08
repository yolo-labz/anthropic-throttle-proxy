# 332 — dependency/security shipping repair

Date: 07/10/2026. Generator family: openai. Isolated from branch314/Actions PR315.
Base: `859ba6c62d3846d0ee3751b5368351f27c58c84d`. No source/runtime activation.

## Spec / hypothesis / falsifier

The locked multidict 6.7.1 C extension leaks operand-value references in reflected
items-view union and subtraction; first-fixed 6.9.1 removes the demonstrated leak.
GHSA-54p9-h82j-f925 / CVE-2026-104874 lists affected >=6.7.0,<6.9.1.
Falsify by showing no leak on the installed locked extension, or persistence on
6.9.1. Use one operand/one operation per case, not a memory-growth stress test.

The first concrete remaining source-only defect is mutable external image tags
in Dockerfile. Resolve their current multi-platform registry manifest digests;
preserve tags/versions/stages while making inputs immutable. No Actions/CODEOWNERS,
gate/exclusion/suppression changes. Other CodeQL/Scorecard findings are ranked in
the source report, not dismissed to inflate a score.

## Plan / tasks

- [x] Preserve old worktree and PR315; create fresh feature worktree.
- [ ] Save canonical advisory and registry receipts; run failing baseline tests.
- [ ] Update only multidict in uv.lock to 6.9.1; pin Docker image digests.
- [ ] Run targeted regressions, full admitted pytest, Ruff and normal hooks.
- [ ] Run actual exact-head CI/security scans, verify unresolved reviews and
      protected safe-class permissions, ordinary PR + squash merge.
- [ ] Publish source report and canonical SAVE-STATE in isolated Notes worktree.

Constitution: no proxy algorithm, identity format, health, limiter, UI or bearer
handling changes. No vendor SDK/dependency additions. One runnable small regression
module covers the actual leak, shipped lock and immutable build inputs.
Revert path: one normal PR reverting the squash commit. Code-only safe-class merge;
production deployment/activation is neither requested nor performed.
