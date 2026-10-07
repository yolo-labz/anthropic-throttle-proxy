# Bound gauge-view document overflow — 07/10/2026

Base: current `origin/main` `de88b98` (#319/#320/#321 included). Isolated worktree/branch `323-ui-scroll-geometry`; prior merged 317 worktree/evidence/optional probe untouched.

## Failure and scope
QA confirmation reproduced synthetic workload document widths 473 at 390px and 1391 at 1366px; old deployed UI fits. Outside-table rectangle census found nothing, falsifying the metadata-width hypothesis. This source/browser defect is not a quota/throughput or producer issue.

## Provisional hypothesis / falsifiers
Hypothesis to test, not ship from similarity: absolutely positioned visually-hidden annotations inside table scroll regions lack a local containing block, so their scrollable boxes escape the scroller and enlarge the root while table visual boxes remain contained. Falsifiers: no escaping positioned descendant, changing only the region's containing block does not remove document overflow, or expected scroll/focus/content behavior regresses.

## Acceptance
- Preserve fail-before screenshot/DOM/geometry and source hashes at narrow 390 and desktop 1366; reuse `tests/check_gauge_wire.py`, not another browser harness.
- Identify a specific causal descendant/rule with computed geometry and bounded reversible browser perturbations before CSS change. No blanket root clipping, metric suppression, class rename or removed content.
- Minimum CSS correction at the real containing-block/layout seam, keeping table scrolling, keyboard focus/HTMX state, readable text, gauge units/freshness and all existing interactions.
- Runnable browser regression must pass actual narrow+desktop root geometry and local scroll-container/content/focus assertions, including measured/stale/error/local-unknown and final sibling selection.
- Admitted browser, source tests/Ruff and exact-head normal PR/CI through safe merge. Deployment/live browser remains pV/pW custody and separately accepted.

## Non-goals
No backend, collectors, credentials, runtime/deploy, JS framework/dependency, speculative navigation/touch redesign, test thresholds/exclusions or quota semantics.
