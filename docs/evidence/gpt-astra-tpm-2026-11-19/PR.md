Direct Pi `openai-api/gpt-6.2-astra` traffic bypasses the gateway and the installed subscription continuity wrapper. Separate workers can consume the same remaining organization TPM and collide again after a short Retry-After.

This change adds a native-fetch adapter with SQLite reservations shared by organization/rate group on one host. Explicit rate rejections wait for the longest upstream delay, then reacquire the budget and resend the identical buffered request. The adapter preserves provider, model, effort and authentication; native Pi still owns SSE and terminal handling.

## Diff explanation

`clients/openai-token-admission/admission.mjs` reserves a conservative token bound transactionally before each API send and shares native quota observations and retry holds across workers. `clients/openai-token-admission/pi-extension.ts` wraps the existing direct Astra API provider using explicit private configuration while preserving model selection and authentication. `clients/openai-token-admission/admission.test.mjs` exercises parallel workers and contention with no real network, while the conservative byte bound can delay work longer than upstream hints and cannot govern uninstrumented clients or other hosts.

## Validation

Validation: 31 synthetic checks pass, including the exact installed Pi Responses provider, six independent workers, the 4M/3.95M/106848 case, 739/904 ms rejections, restart state, in-flight expiry, crashed senders, SQLite contention, SSE completion, incomplete streams and cancellation. The same capacity oracle fails with admission removed. Child workers have empty homes, synthetic credentials, IPC upstreams and a global fetch that rejects real network calls. The final frozen Python suite passes all 3211 tests in 77.51 seconds after the in-flight expiry fix. Ruff and quality-gate evidence is recorded alongside the synthetic logs.

No production requests, live load tests, gateway policy changes, model migrations or subscription moves are part of this change. Deployment still requires explicit organization configuration and loading the extension in affected API workers at safe boundaries; this PR does not claim fleet activation.

Revert: one `git revert` of the squash commit; existing running processes are unaffected until an explicit extension deployment.
