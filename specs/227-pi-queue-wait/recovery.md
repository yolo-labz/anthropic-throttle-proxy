# Admission-wait recovery — 14/09/2026

## Candidate, not delivered

Transport/extension implementation: GLM-5.3-Flash. The corrected validator and this recovery record contain OpenAI-authored work. A Codex review may independently gate the GLM transport only; it cannot approve its own family's validator edits. Final mixed-family release review remains blocked on a permitted different-family reviewer. No extension installation, live inference, provider dispatch, service restart, merge or deployment is claimed.

## Evidence sequence

1. Initial worker candidates: 28/51 tests passed. The inherited claim that all 23 failures were transport defects was falsified by controller execution and an isolated Codex/Sol review.
2. Four negative status cases demanded 503 for 401/403/429/500; native error fixtures omitted role and real nested usage/cost; CommonJS resolution could not select the ESM-only compat export; provider catalog IDs were mistaken for registered IDs; native model/serialization fixtures were incomplete.
3. The incorrectly briefed transport repair1 was stopped before edits. GLM test repair1 later ended with output-limit termination and no file edits. Both attempts remain recorded; retry counts were not reset.
4. Bounded Codex test repair produced real files but hit its 15-minute deadline. Controller ran them outside the restricted child sandbox, preserved the native trailing newline, and recorded 61/76 passing with 15 deterministic transport failures. Tests are committed as `069ee92` in the test worktree.
5. Final transport repair2 (GLM, high effort, 15-minute hard cap, only queue-wait.mjs writable by contract) changed the six independently identified mechanisms. Controller staged its exact output into the frozen validator worktree: **76/76 passed, 0 failed, 0 cancelled**, including all six native adapter/loader cases. `node --check` also passed. No repair3 is authorized by this slice.

## Validated transport hash

`queue-wait.mjs` SHA256:
`7b5e55d6508a1c4d4fa546e038ea8df3100de07ace5ca4889ac67c0c1609a3d5`.

Changes: strict full request/final URL; numeric injected clock for HTTP-date Retry-After; required nested cost fields; original error propagation instead of empty stream end; token-scoped internal-fetch clone verdicts; cancellation while a clone body is stalled.

All requests in the validator use a synthetic key and a loopback fake server. This is source/integration evidence, not a live-user turn-recovery receipt.

## Runnable check

Set `PI_CODING_AGENT_ROOT` to an installed Pi 0.84.4 package directory, then run:

```sh
node --check clients/pi-queue-wait/queue-wait.mjs
node --test clients/pi-queue-wait/queue-wait.test.mjs
```

The test worktree contains the controller-staged transport snapshot. The final candidate must contain all three implementation files and both test files together. Missing native dependencies must fail, never skip the native cases.

## Remaining gates / debt

- Exact-candidate independent transport review; do not infer ALLOW from the 76 tests.
- Independent review of OpenAI-authored validator changes before a mixed-family release.
- No production/desktop activation without its separate authorized guarded workflow.
- Hook warnings recorded: native-replay fixture size, nesting, long test-registration function and decorative comments. Hooks passed without bypass; these warnings were not converted into false correctness failures or silenced.
- Original transport-plan history contains superseded claims; its phase-2 amendments are authoritative over its older planning text, not evidence of current deployment.

Detailed private controller logs and worker sessions remain outside Git under fleet-coordination's existing `evidence/glm-queue-swarm-2026-09-12/` directory.
