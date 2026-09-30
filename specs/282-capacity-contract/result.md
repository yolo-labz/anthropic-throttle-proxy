# Result 282 — capacity contract + synthetic fairness check

Branch `282-capacity-contract` · base `2e7a43f` · scope per `assignment.md`
(supersedes the editx assignment; no production code, accounts, private vault,
deployment, pushes or additional workers were touched).

## SHA

- **Work commit (deliverable): `19313dbb5783f67287a7518dd6fd18e6544dc57e`**
  `feat(spec): bound capacity contract + synthetic fairness check` — 5 files,
  +463 lines (this file is the follow-up commit that records the SHA).
- Hooks: lefthook pre-commit ran (code-slop gate PASS; alignment gate initially
  rejected `# noqa` suppressions — root-fixed by switching the env-pinned dynamic
  imports to `importlib`, then clean).

## Deliverables

| File | What |
|---|---|
| `specs/282-capacity-contract/spec.md` | Bounded contract: eligibility + eligibility-normalized utilization vs equal raw requests; accounting rules C1–C6 (no mixed-window sums, no unit mixing, unassigned ≠ active quota, no runway claim, reserved lanes preserved); fairness invariants F1–F6 bound to existing code; throughput rules T1–T4 with falsifiable tuning targets + halt conditions. |
| `specs/282-capacity-contract/plan.md` | Why the smallest missing piece is the contract + one check over the existing `FairBearerLimiter`/`budget_paced`/`least_loaded`/plan-meter/pool primitives (no scheduler rewrite). |
| `specs/282-capacity-contract/tasks.md` | Speckit task list (all checked). |
| `scripts/check-seat-fairness.py` | Runnable deterministic synthetic check driving the REAL `FairBearerLimiter` (imported from `src/`, public API only). |

## Executable evidence

Command: `uv run python scripts/check-seat-fairness.py` → **rc=0**,
`RESULT: PASS — all spec-282 invariants hold (simulation)`. 21/21 assertions PASS:

- phase1 (F1/F2): dispatch order `['A','B','A','B','A','A','A','A','A','A']` —
  8-request chatty client interleaves 1-for-1 with the 2-request sibling, which
  completes within the first 2k=4 dispatches.
- phase2 (F3/C5/T1): `completed={'X': 6, 'Y': 6, 'Z': 0}` — unavailable seat gets
  zero work; `overlap_max={'X': 2, 'Y': 1}` equals each live cap; equal raw split
  (6/6) yields **unequal normalized utilization `{'X': 0.75, 'Y': 1.5}`** — the
  measured demonstration that equal raw requests ≠ eligibility-normalized
  utilization. SIMULATION timings (informational): wall 73.8 ms, completed 12,
  mean_overlap 1.33, queue_wait p50 8.30 ms / max 61.39 ms, busy X 24.9 ms /
  Y 73.4 ms.
- phase3 (F4): cancelled waiter `c1-cancelled` never acquired; order preserved
  `['c1','c2']`; `inflight=0`, `_holds` and `_queues` empty — no slot/lease leak.
- phase4 (F5): `QueueWaitTimeout` raised with no slot held by the rejected
  request; lease bookkeeping clean afterwards.
- phase5 (F6): shrink sequence `[2, 1, 1, 1, 1, 1, 1]` never below `AIMD_MIN=1`.
- phase6 (C1/C3/C5/T1/T3): mixed-window sum refused (ValueError); same-window sum
  allowed; `active_quota=2` — unassigned/stale/unknown/exhausted seats excluded;
  cap-raise without measured overlap inadmissible; any 429 halts tuning.

Lint: `uv run ruff check scripts/check-seat-fairness.py` → `All checks passed!`

Re-run: `git checkout 19313dbb -- . && uv run python scripts/check-seat-fairness.py`
(idempotent; state created in-process only).

## Boundaries honored

No `src/`/`tests/`/lane/credential-pool/deployment changes; no push/merge (coordinator
integrates to branch `279`); Z.AI reserved slots and Pi behavior untouched; every
result labeled SIMULATION — no provider benchmark, real inference, account access,
cross-window extrapolation or monthly-runway claim.
