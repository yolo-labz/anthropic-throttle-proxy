# 287 — deliver the minimal queue-expiry hotfix independently

30/09/2026: a new screenshot reports 8 local queue refusals, 1,693,282 ms sleeping and no streamed model. The installed client still has 1,800,000 ms / 32-retry defaults. Private transcript and screenshot remain in the private vault. The affected pane has since resumed on its own; do not interrupt it.

Hypothesis: finite default admission budgets, not upstream quota, manufacture this specific terminal failure. Falsifier: actual native replay under default settings keeps waiting past both ceilings without the change. Existing provenance/usage and cancellation gates remain immutable.

## Minimal plan

1. Reuse actual Pi 0.85.1 + existing local fake upstream to reproduce both old ceilings and deep-wait cancellation (MiMo and ZAI). No live inference.
2. Change only default total-wait/rejection ceilings to Infinity; preserve explicit positive finite DI ceilings, pacing, cancellation, stream/usage/provenance guards and all provider configuration. No proxy/server cap/timeout or new dependency.
3. Run complete native/unit replay, full pytest and ruff. Deliver separate protected-base PR; no Python service rollout.
4. Advance the separate Nix client pin with its verified filtered-source hash, then activate only through a safe authorized path and idle-boundary reload. A source merge alone is not a runtime fix.

## Coordination

This coordinator-owned worktree does not modify the active 286 worker's worktree or interrupt its session. 286 is still building extended acceptance; integrate useful nonduplicate tests later. The recurrence must not wait for the unrelated telemetry/dashboard/pool changes in 279. Generator family: OpenAI. No automated approval is claimed; prior GLM admission refusal must not be retried as a gate.

## Tasks

- [x] Native failing reproduction: six new cases fail on the old defaults (`red.tap`).
- [x] Defaults and timer safety: removing the old budget exposes Node's >2^31-1 timer overflow; two additional tests fail before the finite-value check/chunked timer fix (`timer-red.tap`). All 233 client tests pass (`green.tap`), 1,404 pytest tests pass (`pytest.log`), ruff lint and format pass. Native tests cover both MiMo and ZAI, exact request body replay, cancellation beyond the former ceiling and explicit finite bounds.
- [ ] Protected-base PR/CI/merge evidence.
- [ ] Separate client pin, safe activation and runtime evidence.
