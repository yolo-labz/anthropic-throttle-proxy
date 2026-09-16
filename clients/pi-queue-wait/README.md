# pi-queue-wait

A Pi extension that keeps a **stamped queue-timeout rejection** from a local
[anthropic-throttle-proxy](../../README.md) z.ai lane pending and retries it
after the advised delay, instead of losing the turn to a transient admission
failure (spec 227).

## What happens without it

When the proxy's fair queue is saturated, it fails a request fast with:

- HTTP `503`
- `x-anthropic-throttle-proxy: 1` (proxy provenance)
- `x-anthropic-throttle-queue-timeout: 1` (this proxy generated the queue timeout itself)
- body exactly: `proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue\n`
- `Retry-After` set to the proxy's drain estimate (default-configured bounds:
  floor 5 s, cap `THROTTLE_QUEUE_RETRY_AFTER_MAX_S`, default 300 — a knob, not
  an immutable universal cap)

The native provider path surfaces this as an assistant error and the turn dies.
The proxy intends the clean 503 to be retried; this extension performs that
retry, transparently.

## What it does

- Intercepts **only** `provider=zai`, models `glm-5.3-flash` and `glm-5.3`, POSTs to a
  permitted loopback HTTP endpoint (default `http://127.0.0.1:8766`) with the
  exact path `/api/coding/paas/v4/chat/completions`.
- Treats a response as a queue rejection only when **all** hold: status 503,
  both stamps exactly `1`, no redirect (final URL equals the request URL),
  body byte-equal to the sentence above plus its trailing newline, and the
  native error event carries empty content with **every** usage and cost field
  zero, both terminal reasons are exactly `error`, and no earlier event of any type was emitted.
- Waits `Retry-After` plus strictly positive jitter (1–1000 ms), then
  re-dispatches **the same model request** — same model, context, options.
  Missing/unparsable/non-positive `Retry-After` falls back to 15 s.
- Every other response or error — auth/quota failures, a raw 503 without the
  stamps, foreign origins or redirects, partial streams with any prior event,
  any non-zero usage/cost — passes through the native path unchanged.

## What it never does

- Never touches the proxy, its semaphore, or its queue.
- Never retries after real output: no retry once any stream event was emitted,
  after an abort, or when usage/cost is non-zero.
- Never logs bearer tokens or request bodies.
- Never retries a non-queue error (429s, quota, overflow keep their native
  semantics, including the native SDK's own pushback handling and the caller's
  `maxRetries`, which are preserved untouched). For a fully proven queue rejection
  only, a response-header overlay (`x-should-retry: false`) prevents native
  retries from bypassing the outer wait budget. The original headers stay untouched.
- No toggle commands (`queue-wait-off` / `queue-wait-on` do not exist):
  disabling is removing/reloading this extension only. Unregistering `zai` at
  runtime could erase another extension's merged provider overlay.

## Candidate status

**Not approved for installation pending exact-head review.** The original
candidate was denied despite 76 passing tests. A newly bounded
[retry-safety follow-up](../../specs/232-pi-queue-retry-safety/plan.md) addresses
native retry accounting, terminal semantics, clock-failure reporting and abort
identity, with both GLM models covered. The expanded tests failed 10 cases before
the fix and pass 89/89 afterward. The [original DENY](../../specs/227-pi-queue-wait/transport-review.md)
is preserved; green tests do not replace the different-family release gate.

## Install (after release gates pass)

Copy this directory into your Pi extensions directory:

```sh
mkdir -p ~/.pi/agent/extensions
cp -r clients/pi-queue-wait ~/.pi/agent/extensions/pi-queue-wait
```

No build step, no new runtime dependency: `index.ts` is loaded by Pi's jiti
loader and imports the sibling plain-ESM `queue-wait.mjs`. The native adapter
is imported from `@earendil-works/pi-ai/compat`, the loader-safe public entry
that re-exports the OpenAI-completions factory.

Registration is
`pi.registerProvider("zai", { api: "openai-completions", streamSimple })` —
no models/baseUrl/apiKey/headers — so your configured z.ai auth, endpoint,
models, and request options are preserved.

## Knobs

All ceilings live in `QUEUE_WAIT_DEFAULTS` (`queue-wait.mjs`) and can be
overridden through `createQueueWaitStream(deps)` (dependency injection only —
there is no config/env surface in v1):

| Dep | Default | Meaning |
|---|---|---|
| `maxWaitMs` | `1_800_000` | Total admission-wait budget in ms. Bounds sleeps only — never an already-started successful stream. Exceeding it ends the request with a synthetic error. |
| `maxRejections` | `32` | Number of queue rejections that are each followed by exactly one re-dispatch. The `(maxRejections + 1)`-th rejection ends the request with the synthetic give-up error — never the original stamped 503. |
| `fallbackRetryAfterMs` | `15_000` | Wait used when `Retry-After` is missing, unparsable, or ≤ 0 (conservative, non-hot). |
| `jitterMaxMs` | `1_000` | Strictly positive jitter bound added to every wait. |
| `allowedBaseUrls` | *(unset)* | **DI-only test seam.** Exact `http://` loopback base URLs whose origin is additionally eligible. Never set it in production wiring; omission keeps the strict `:8766` default. |
| `onWait(info \| null)` | no-op | Counters-only wait signal: `{ attempt, delayMs, retryAfterMs, fallback, waitedMs }`, `null` clears. |

Exhaustion/abort surface as ordinary assistant errors whose text is explicit
and contains no generic retry triggers (`503`, `rate limit`), so nothing above
the model layer re-enters a hot loop:

- `queue-wait: admission wait budget exhausted after N queue rejections and X ms of waiting; the model was never streamed`
- `queue-wait: gave up after N queue rejections (limit M); the model was never streamed`

## Safety envelope

- The request-URL gate requires `http:`, a loopback host (`127.0.0.0/8`,
  `::1`, `localhost`), the exact completions path, port `8766` (or an
  explicitly allowed test base), and that the final response URL matches —
  a redirect can never carry the stamps into the verdict.
- The sleep happens only after the native attempt has fully ended, so native
  per-attempt HTTP timeouts are done before waiting; the parent AbortSignal
  stays live through the sleep and aborting emits no retry.
- Concurrent requests are fully isolated (all state is per-call closure).
- If the operator's lane is not the expected URL, the extension silently
  passes everything through — the safe default.

## Relation to the proxy

The extension recognizes exactly the queue-timeout response documented in
`src/anthropic_throttle_proxy/` (`MARKER_HEADER`, `QUEUE_TIMEOUT_HEADER`,
`QUEUE_TIMEOUT_RETRY_AFTER_S` floor, `THROTTLE_QUEUE_RETRY_AFTER_MAX_S` cap —
a default-configured bound, tunable). It does not weaken any proxy-side
enforcement: the retry re-enters the fair queue like any other arrival.
