# Independent final-source/browser acceptance — 07/10/2026

## Scope

New isolated worktree `325-ui-qa-final`; preserve merged 319/321 branches/WIP.
Read the canonical campaign, pinned Impeccable 4.5.0/engine 0.1.11 and browser
INDEX/Cookbook. Existing checker and declared delivery dependency only. No new
agents, styling/backend edits, runtime mutation or unadmitted browser.

Candidate #323: `4862145dcaff3a1d499e8d90cc34f0e29dada96c`.
Merged final source: `45b7d0ef3321cca9c6d4e8513686efa30d889c1a`.
#321 is already MERGED `de88b981`; its pending footnotes are historical.

Hypothesis: #323 makes clipped quota annotations scroll-local while preserving
accessible text, table scrolling and all existing interaction/state semantics.
Falsifiers: document overflow at 390/1366; absent/ignored quota accessibility
text; escaping annotations; source/focus/detail loss across polls; stale/error
becomes numeric zero; render network or console errors.

## Tasks / executable acceptance

1. Confirm final UI/checker hashes against exact candidate and fail-before
   receipts, not a shared current dashboard or implied Git revision.
2. Extend only the existing checker with Chromium accessibility-tree receipt for
   existing hidden quota text. One admitted independent source reproduction,
   sequential 1366/390 widths, ephemeral headless installed Chromium, GPU off.
   Preserve all original nonzero geometry and state/network oracles.
3. Save sanitized PNG/DOM/AX, exact source/checker manifest and command status
   under `docs/evidence/ui-browser-qa-2026-10-07/final-source/`.
4. Normal hooks/PR/exact-head CI for receipt/test delta, then safe source-only
   merge. pT owns #323, pV alone activates the immutable service/timer build.
5. Prepare a separate read-only deployed-build mode in the same checker: prove
   served UI source/build bytes, real HTMX poll/focus/detail stability, geometry
   and report units/freshness. Never apply synthetic collector/cache patches to
   live acceptance; preserve synthetic stale/error coverage separately. Run only
   after pV supplies/activates its exact final build, through admitted compute.

Durable report: `docs/UI-BROWSER-QA-2026-10-07.md`; coordination: canonical
UI-TEAM note. No source/browser/deployment approval fabricated. No blind restart.
