# P0 — admission waiting must not discard unstarted work

## Hypothesis and falsifier

Hypothesis: the local Pi adapter's finite default admission-sleep/rejection budgets manufacture a terminal error while an otherwise retryable request has not begun streaming.

Falsifier: the reported terminal message is not emitted by that default-bound path, or the captured refusal has prior stream output/nonzero usage/non-queue provenance. Those cases must not receive blind replay.

## Evidence

Public software error supplied by the operator: `queue-wait: admission wait budget exhausted after 9 queue rejections and 1735104 ms of waiting; the model was never streamed`.

Source at integration HEAD `8789b99`: `clients/pi-queue-wait/queue-wait.mjs` checks `waitedMs + delayMs > maxWaitMs` before sleeping and synthesizes this message. Defaults are 1,800,000 ms of accumulated sleeps and 32 retried queue rejections (the 33rd rejection terminates). This is not evidence of an upstream quota rejection or of current Team saturation. Private transcript/screenshot receipts remain in the private project vault, not this repository.

On 30/09/2026 the coordinator ran the existing native Pi 0.85.1 bounded-path fixtures:

```sh
PI_CODING_AGENT_ROOT=/path/to/pi-coding-agent \
  node --test --test-name-pattern='mimo native: bounded' \
  clients/pi-queue-wait/mimo-replay.test.mjs
```

Result: **3 tests passed** (first sleep over budget, cumulative sleep budget, rejection ceiling), 0 failures, 0 skipped. These tests deliberately expect today's terminal errors; they confirm the mechanism, **not the new acceptance or a fix**. Native adapter + fake loopback only; no inference/production service was called.

## Acceptance and ownership

Existing workspace worker `w1P:p2` completed 282 and received isolated `286-persistent-admission`. Its first dispatch aborted before tool use; one scoped resumption was sent. Assignment delivery is not implementation acceptance.

- Production defaults keep a proven pre-stream queue rejection pending until admitted or cancelled, beyond 30 minutes and 32 refusals.
- Native success after those waits, cancellation, concurrent isolation and unchanged non-queue handling must have executable regressions with injected sleeps.
- Preserve exact origin/provider/path/stamps/body, zero usage/cost, no-prior-event and native retry-accounting guards. No partial-stream replay, quota/auth bypass, hot retry loop or model substitution.
- Do not raise the server's silent queue wait beyond Pi's idle timeout.
- Coordinator integrates the tested source and advances the separate Nix client pin; no running-tab reload/activation has happened.
