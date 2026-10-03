# Integration plan

Base: `2e7a43f`. Reuse existing static pools, account selection, FairBearerLimiter, schema-1 lane reports and server-rendered HTMX UI. No new dependency or scheduling framework.

## Parallel ownership
- 281 / w1P:pF: MiMo probe, lane normalization and their existing tests.
- 282 / w1P:p2: policy contract and a synthetic fairness/throughput executable.
- 283 / w1P:pC: shared account selection and existing routing/account tests.
- 284 / w1P:pD: new deterministic `test_seat_capacity_acceptance.py` only, plus its spec.
- 285 / w1P:pE: UI/presentation/templates and UI tests only.
- 279 / coordinator: integrate exact commits, executable acceptance, PR/CI and runtime evidence. Private account evidence stays outside public worker packets.

Each worker has a sibling worktree, a bounded assignment, no deployment rights and no extra delegation. Commit with normal hooks; coordinator cherry-picks verified slices and owns PR delivery. Existing dirty main/229 worktrees stay untouched.

## Integration seam
Preserve `mimo:plan`; add `mimo:team-owner` in schema 1 with allowlisted counters/expiry and distinct identity. The Nix consumer currently requires exactly one individual row and binds its classifier to that old row; a separate Nix worktree updates validated project-ID forwarding, exact report identity validation and active-meter selection. Pin only an accepted upstream revision with its verified fixed-output hash.

## Falsifiable hypotheses
- The UI's individual-only path and transport-only healthy label hide real usable capacity: falsify with fixture rendering that already distinguishes both correctly.
- An unconfigured incoming bearer can beat the configured static pool on occupancy: reproduce with retired A and busy usable B before editing selection.
- Safe parallelism increases useful completion rate: demonstrate with bounded local fake upstreams, then compare actual completion/queue/refusal/burn windows after authorized activation. Do not infer a provider cap or monthly runway from a synthetic test.


## Exact-bearer backoff follow-up — 03/10/2026

Hypothesis: the legacy all-sibling meter fallback misclassifies fresh Team B's
headerless 429 as exhausted budget when A is spent. The new regression fails
against merged `7236d96` at `_budget_under_pressure({}, "bid-b")`, returning
true where only B's own fresh row should clear the budget inference.

Plan and tasks: reuse the existing credential-label binding resolver; dispatch
between explicit-binding and legacy-unconfigured regimes; pass the actual
bearer through classification; preserve unified response/cache precedence;
prove A cannot borrow B's meter and malformed, stale or unknown rows fail closed.
The owner froze two source/test files; Mac handles executable validation and
publication. No new quota or provider cap is invented.

Implemented in `proxy.py` and `test_plan_lane_pushback.py`: **108 focused tests
passed**, including the red-before/green-after regression. Full-tree Ruff lint
and format checks passed. Existing desktop sessions and runtime remain intact.
This changes backoff classification, not admission policy or account selection.
Rollback is one revert PR. The full suite passed **1,627 tests**. The protected
Astra seat is independently reviewing this MiMo-authored slice; no approval is
asserted before that receipt arrives.

CI's clone ratchet caught a duplicated row factory in the new fixture. The
fixture now reuses the existing sibling-snapshot helper with optional lane
identity/status arguments; defaults and production code are unchanged.

Independent Astra review of the MiMo-authored source found one P2 test gap:
the cached-unified precedence case exhausted only A, leaving B fresh. The
fixture now exhausts B's own row, so ignoring B's cache would fail the test.
No production defect was reported. The repaired focused suite passes.
