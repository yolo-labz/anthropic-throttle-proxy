# Transport-only independent review — 14/09/2026

**DENY**, exact reviewed head `d3e056cd1db0be141993f3fe35990daf485f0130`.
Reviewer: isolated Codex/Sol; generator family of the transport and extension: Chinese-frontier. This does NOT certify the OpenAI-authored validator or recovery documents, nor the mixed-family release.

The controller's assembled candidate passes 76/76 tests, including six actual Pi adapter/loader cases against synthetic loopback servers. Python regressions: 1045 passed; ruff check/format passed. Those checks do not discharge the findings below.

## Release blockers

- **P1 — native SDK retries bypass outer admission accounting** (`queue-wait.mjs:260,323`). The implementation correctly preserves caller `maxRetries` per the approved plan, but native retries occur before the wrapper sees a terminal event. With `maxRetries > 0`, a stamped 503 can lead to another HTTP request without the wrapper's positive jitter and without counting against its wait/rejection ceilings. Existing native cases set `maxRetries: 0`; the option-propagation unit test is not a real native retry. Required follow-up must reconcile BOTH promises (preserve unrelated native retry behavior AND bound stamped queue retries), not globally force `maxRetries=0` or silently weaken the budget claim. Minimum regression: actual adapter, `maxRetries: 1`, stamped rejection then success; require accounted, jittered admission wait.
- **P1 — terminal semantics fail open** (`queue-wait.mjs:292,432`). Eligibility excludes `reason === "aborted"` but does not require both `event.reason === "error"` and `error.stopReason === "error"`. The reviewer reproduced a stamped rejection plus a zero-usage error event carrying `reason/stopReason: "stop"`: two calls occurred and only the second done event surfaced. Unknown/cancellation-like native drift must pass through, not retry.

## Additional findings

- **P2 — throwing clock can still erase the terminal event** (`queue-wait.mjs:364,382`). Recovery calls injected `now()` again; a repeated throw is swallowed before `outer.end()`. Reviewer repro produced zero events. Use a safe terminal timestamp path while preserving the original error.
- **P2 — message-only abort classification** (`queue-wait.mjs:405`). An ordinary sleep `Error("aborted")`, with no aborted signal/AbortError identity, is rewritten to cancellation and loses its original error semantics.
- **P2 documentation**: installer directory and README relative link were wrong. Corrected after review to `~/.pi/agent/extensions` and `../../README.md`; this does not resolve either code blocker or create an ALLOW at a new head.

## Scope and stop

Reviewer checked exact head/base/hash and all 70 pure unit cases. Native execution was unavailable inside the read-only review sandbox (`EPERM` loopback, `EROFS` temp creation); controller's native results remain separate evidence. No live provider/auth/deployment used.

Transport repair ceiling **2/2 exhausted**. No repair3, reset of counters, same-family release verdict, or waiver is authorized by this slice. This is a blocked draft candidate for a newly bounded follow-up; no installation/activation is implied. Mixed-family validator review also remains unavailable. Preserve this denial alongside the green tests.
