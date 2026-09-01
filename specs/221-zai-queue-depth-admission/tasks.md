# Tasks: queue-depth admission + honest queue-timeout Retry-After

Ordered, one producer slice. RED before GREEN.

- [x] **T0 — Falsify or confirm.** Deterministic replay of the measured shape
  against current `main`. Result recorded in `evidence.md`. (Not falsified.)
- [x] **T1 — Freeze artifacts.** `spec.md`, `plan.md`, `tasks.md`, `verify.sh`,
  `evidence.md` committed before any `src/` edit.
- [x] **T2 — RED test.** `tests/test_queue_depth_admission.py` drives the real
  `FairBearerLimiter` seam with 2 slots occupied by long holders, 3 queued, and
  a short budget; asserts pre-queue rejection, unchanged `queued_total`, no
  slot consumed, and a truthful `Retry-After`. Must fail on `main`.
- [x] **T3 — Config.** `THROTTLE_QUEUE_DRAIN_DEFAULT_S` (10.0) and
  `THROTTLE_QUEUE_RETRY_AFTER_MAX_S` (300) in `config.py`, documented inline
  with the incident that motivates them.
- [x] **T4 — Estimator.** Per-slot LEASES (`_holds: lease -> (lane, start)`)
  opened at every `inflight += 1` and returned at every `inflight -= 1`,
  including `_cancel_cleanup` (return without sampling). An unknown lease takes
  nothing; a leaseless caller falls back to the oldest hold in its lane.
  Completed durations go to a bounded per-lane sample deque.
- [x] **T5 — Drain estimate.** `FairBearerLimiter.drain_estimate(max_wait,
  priority=..., client_id=...)` returning service time, per-holder residual,
  provenance, sample count, slots, free, queued, the round-robin `ahead` rank,
  scheduled `wait_s`, `max_depth`, `admits`, `rejects`, and `retry_after_s`.
- [x] **T6 — Reject pre-queue.** `_FairSlotContext.__aenter__` consults the
  estimate before `acquire()`; `QueueWaitTimeout` carries `pre_queue` + the
  estimate. No new handler control flow in `proxy.py`.
- [x] **T7 — Honest Retry-After.** `_queue_wait_timeout_response` emits the
  bounded integer drain estimate and logs the reason (`pre-queue-depth` vs
  `elapsed`) with the estimator inputs.
- [x] **T8 — Publish.** `snapshot()["drain"]` + `_lane_saturation` aggregation
  into `saturation.queue_admit_max_depth` / `saturation.queue_admit`, with the
  config-derived cold fallback when nothing is measured.
- [x] **T9 — GREEN + edges.** Cold/no-history, zero and disabled bound,
  `off`/`observe` mode, priority reserve lane, cancellation cleanup leaves no
  sample and no leak, Retry-After floor/ceiling, admission-endpoint contract.
- [x] **T10 — Gates.** Targeted pytest, full pytest (max parallelism), ruff
  check + format, `git diff --check`, `verify.sh`, Docker/Nix-relevant sanity,
  bounded live-compatible smoke that never touches the running `:8766`.
- [x] **T11 — Codex adversarial review** (different family): causality,
  limiter state, depth math, wait-budget inheritance, cancellation/leaks,
  deployment plan. Repair every blocker/major, re-review at exact head.
- [x] **T12 — PR** (draft → ready), CI green, merge only if the safe-class
  preconditions hold; record the exact gate otherwise. No NixOS pin bump here.
- [ ] **T13 — `fleet-intel attempt zai-queue-depth-admission`** with the
  outcome and the exact remaining blocker for live verification.
