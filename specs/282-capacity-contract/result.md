# Result 282 — capacity contract + synthetic fairness check

Branch `282-capacity-contract` · base `2e7a43f` · scope per `assignment.md`
(supersedes the editx assignment; no production code, accounts, private vault,
deployment, pushes or additional workers were touched).

## SHA

- Original work commit: `19313dbb5783f67287a7518dd6fd18e6544dc57e`
  `feat(spec): bound capacity contract + synthetic fairness check`.
- **Review-fix commit (current deliverable): `5f5b463b995f2545618ca6a6b12112cfdab95826`**
  `fix(spec): dimension-safe aggregation, slots!=quota, honest labels` — addresses
  all five findings of `specs/279-team-capacity/review-282.md` (reviewer: GPT/Astra
  coordinator over MiMo-generated work).
- This file is the follow-up commit recording the SHAs. Hooks: lefthook pre-commit
  ran on every commit (code-slop + alignment gates PASS).

## Review findings → fixes (30/09/2026)

| # | Finding | Fix |
|---|---|---|
| 1 | Same-window percentages summed (40%+30%→70 is dimensionally wrong; equal accounts → 35%). | `add_window_percents` **removed**. `aggregate_window_percent` aggregates ONLY as `Σused/Σlimit` with strict window+unit identity; bare/unbound percentages cannot be expressed/aggregated. Executable assertion: equal accounts 40%/30% → **35.0**, and mixed-window, mixed-unit, unbound-denominator readings all **refused**. Spec C1 rewritten accordingly. |
| 2 | `active_quota` returned concurrency slots called "quota". | Renamed **`eligible_slots`**; assertion and spec now state explicitly: concurrency slots, subscription quota and throughput are distinct dimensions (new Dimensions rule in spec §2; C3 keeps "quota" for subscription capacity only). |
| 3 | Phase-2 toy roster ≠ production-router proof; phase-6 helpers ≠ running guards. | Phase 2 labeled **illustrative scheduling** everywhere (assertion text: "not production-router proof; completed=…"), spec F3 + §6 point to the dedicated **284 test suite** as the owner of handler/router evidence; phase 6 header says **contract model functions (not running-system guards)**. Real-limiter phases 1/3/4/5 preserved unchanged. |
| 4 | "21/21" manually maintained; ruff format not run. | The check now **emits its own count** (`RESULT: PASS — 24/24 assertions hold (simulation)`); `ruff format` run (1 file reformatted) + `ruff check` clean. |
| 5 | `git checkout SHA -- .` rerun recipe was destructive. | Removed. Rerun from the checked-out branch worktree: `uv run python scripts/check-seat-fairness.py`. |

## Deliverables

| File | What |
|---|---|
| `specs/282-capacity-contract/spec.md` | Bounded contract: eligibility + eligibility-normalized (concurrency-share) utilization vs equal raw requests; **Dimensions rule** (slots ≠ quota ≠ throughput); accounting rules C1–C6 (C1 = Σused/Σlimit only under window+unit identity; unbound denominators never aggregate; no unit mixing; unassigned subscription capacity ≠ active quota; no runway claim; reserved lanes preserved); fairness invariants F1–F6 with 284-ownership notes; throughput rules T1–T4 with falsifiable tuning targets + halt conditions. |
| `specs/282-capacity-contract/plan.md` | Why the smallest missing piece is the contract + one check over the existing primitives (no scheduler rewrite). |
| `specs/282-capacity-contract/tasks.md` | Speckit task list (all checked). |
| `scripts/check-seat-fairness.py` | Runnable deterministic synthetic check driving the REAL `FairBearerLimiter` (imported from `src/`, public API only) + the dimension-safe accounting models. |

## Executable evidence (run at `5f5b463b`)

Command: `uv run python scripts/check-seat-fairness.py` → **rc=0**,
script-emitted verdict: `RESULT: PASS — 24/24 assertions hold (simulation)`.

- phase1 (real limiter, F1/F2): dispatch order `['A','B','A','B','A','A','A','A','A','A']` —
  8-request chatty client interleaves 1-for-1 with the 2-request sibling, which
  completes within the first 2k=4 dispatches.
- phase2 (illustrative scheduling, C5/T1): `completed={'X': 6, 'Y': 6, 'Z': 0}` —
  toy scheduler only, NOT production-router proof (284 tests own that);
  `overlap_max={'X': 2, 'Y': 1}` equals each live cap; equal raw split (6/6) yields
  unequal **concurrency**-normalized utilization `{'X': 0.75, 'Y': 1.5}`.
  SIMULATION timings (informational): wall 74.3 ms, completed 12, mean_overlap 1.34,
  queue_wait p50 8.58 ms / max 61.84 ms, busy X 25.4 ms / Y 73.8 ms.
- phase3 (real limiter, F4): cancelled waiter never acquired; order preserved
  `['c1','c2']`; no slot/lease leak (`inflight=0`, `_holds`/`_queues` empty).
- phase4 (real limiter, F5): `QueueWaitTimeout` raised with no slot held by the
  rejected request; lease bookkeeping clean.
- phase5 (real limiter, F6): shrink sequence `[2, 1, 1, 1, 1, 1, 1]` never below
  `AIMD_MIN=1`.
- phase6 (contract models, C1/C3/C5/T1/T3): equal accounts 40%/30% aggregate to
  **35.0** (weighted `Σused/Σlimit`), never 70; mixed-window, mixed-unit and
  unbound-denominator aggregation all refused (ValueError); `eligible_slots=2` —
  unassigned/stale/unknown/exhausted seats contribute no **concurrency slots**
  (not subscription quota); cap-raise without measured overlap inadmissible;
  any 429 halts tuning.

Lint/format: `uv run ruff format scripts/check-seat-fairness.py` (applied) and
`uv run ruff check scripts/check-seat-fairness.py` → `All checks passed!`

Re-run (non-destructive; the branch worktree is already checked out):
`uv run python scripts/check-seat-fairness.py`

## Boundaries honored

No `src/`/`tests/`/lane/credential-pool/deployment changes; no push/merge (coordinator
integrates to branch `279`); Z.AI reserved slots and Pi behavior untouched; every
result labeled SIMULATION — no provider benchmark, real inference, account access,
cross-window extrapolation or monthly-runway claim.
