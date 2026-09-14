# Transport plan — Pi queue-wait extension (spec 227, PHASE 1)

Status: APPROVED WITH AMENDMENTS (14/09/2026) and IMPLEMENTED — see §0. The
amendments override any contradicting statement below.

## 0. Plan-gate amendments incorporated in the implementation

1. **Import path**: `index.ts` imports `openAICompletionsApi` and
   `createAssistantMessageEventStream` from `@earendil-works/pi-ai/compat`
   (verified: `dist/compat.js` star-exports `api/openai-completions.lazy.js`
   and `./index.js`; the jiti alias mangles the `.lazy` subpath). The §3.2
   subpath remains documented as the underlying source but is not imported.
2. **Registration**: exactly
   `pi.registerProvider("zai", { api: "openai-completions", streamSimple })`.
   Verified in `dist/core/provider-composer.js`:
   `validateExtensionProvider` throws when `streamSimple` is supplied without
   `api`, and the override dispatches when `model.api === extension.api`.
3. **Body match**: exact equality with `QUEUE_BODY_TEXT + "\n"` — never
   `includes()`. Prefix/suffix/missing-newline responses are ineligible
   (`QUEUE_BODY_FULL`).
4. **Request checks**: effective method POST, protocol `http:`, exact endpoint
   path, permitted loopback endpoint, `res.redirected === false`, final
   response URL same-location as the request URL.
5. **Native-error eligibility**: EVERY usage and cost field zero (generic
   zero-walk incl. `usage.cost.*`, required keys present) AND no earlier event
   of ANY type emitted (not merely non-terminal events).
6. **Clone race**: capture resets before every delegated fetch; clone taken
   immediately; a per-fetch capture promise is awaited when the terminal error
   arrives; clone/read failure ⇒ ineligible passthrough. The original Response
   is always returned untouched.
7. **Jitter**: `Math.max(1, Math.ceil(random() * 1000))` — strictly positive
   even at `random() === 0`.
8. **Caller `maxRetries` preserved** (no global pin to 0); the per-fetch
   capture reset makes stale internal SDK retries harmless.
9. **maxRejections (precise)**: the first `maxRejections` eligible rejections
   each trigger exactly one re-dispatch; the `(maxRejections + 1)`-th eligible
   rejection emits the synthetic give-up error — NEVER the original 503.
10. **`deps.allowedBaseUrls`**: DI-only test seam (exact http loopback base
    URLs; completion path fixed; all other gates stay mandatory). Never wired
    from `index.ts`; omission keeps the strict `:8766` production default.
11. **No commands, no config export** (Q2/Q3): removal/reload is the disable
    path (unregistering `zai` could erase another extension's merged overlay).
    README references no toggle commands and no config surface.
12. **`THROTTLE_QUEUE_RETRY_AFTER_MAX_S` = 300** is described as the default
    configured cap, not an immutable universal cap.
13. **Q1 accepted**: `FALLBACK_RETRY_AFTER_MS = 15_000` for invalid/missing
    `Retry-After`.

## 1. Goal

A stamped queue-timeout rejection from the local `:8766` z.ai lane
(`503` + `x-anthropic-throttle-proxy: 1` +
`x-anthropic-throttle-queue-timeout: 1`) must keep the current Pi model
request pending and retry it after the advised delay. Every other response or
error passes through the native Pi path byte-for-byte. No proxy change, no
vendor SDK, no new runtime dependency.

## 2. Deliverables (transport worker owns exactly these)

| File | Role |
|---|---|
| `clients/pi-queue-wait/queue-wait.mjs` | retry engine; exports `createQueueWaitStream(deps)` + shared constants |
| `clients/pi-queue-wait/index.ts` | Pi extension; registers the stream override for provider `zai` only |
| `clients/pi-queue-wait/README.md` | install, knobs, safety envelope, failure semantics |
| `specs/227-pi-queue-wait/transport-plan.md` | this plan |

Not owned (test-author worker): `queue-wait.test.mjs`, `native-replay.mjs`,
`test-plan.md`. Section 7 fixes the interface those files may rely on.

## 3. Source evidence (all verified in this checkout / mounted Pi 0.84.4)

### 3.1 Proxy side (what the extension must recognize)

- `src/anthropic_throttle_proxy/config.py`
  - `MARKER_HEADER = "x-anthropic-throttle-proxy"` — stamped on **every**
    response the proxy serves; presence alone proves proxy provenance, NOT a
    queue timeout.
  - `QUEUE_TIMEOUT_HEADER = "x-anthropic-throttle-queue-timeout"` — stamped
    alongside the marker ONLY on the 503 the proxy generates itself when
    `QUEUE_MAX_WAIT_S` (default 30 s) is exceeded in the fair queue.
  - `QUEUE_TIMEOUT_RETRY_AFTER_S = 5` (floor) and
    `QUEUE_RETRY_AFTER_MAX_S = 300` (hard cap): the queue-timeout 503's
    `Retry-After` is the drain estimate, integer, clamped to `[5, 300]`
    seconds. Evidence for: parsing seconds is enough for the live proxy, but
    the parser must still accept HTTP-dates (contract) and a conservative
    fallback for absence.
  - `QUEUE_DRAIN_DEFAULT_S = 10` — context for the advertised delay semantics.
- Contract fixes the exact public body text:
  `proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue`
  (trailing `\n` in the HTTP body; the native OpenAI SDK prefixes `503 ` in
  the derived error message). AMENDED: match by exact equality with
  `QUEUE_BODY_TEXT + "\n"` (exported as `QUEUE_BODY_FULL`) — never
  `body.includes()`; anything else about the gate must be exact.

### 3.2 Pi side (how to observe without reserialization)

All paths relative to `/opt/pi/@earendil-works/pi-coding-agent`.

- `node_modules/@earendil-works/pi-ai/package.json` — exports map has
  `"./api/*": "./dist/api/*.js"`, so
  `@earendil-works/pi-ai/api/openai-completions.lazy` resolves to
  `dist/api/openai-completions.lazy.js`. Verified present.
- `dist/api/openai-completions.lazy.js` — `export const openAICompletionsApi =
  () => lazyApi(() => import("./openai-completions.js"))`.
  `dist/api/lazy.js` — `lazyApi` returns `{ stream, streamSimple }`; each
  wraps the real module behind `lazyStream`, which returns a real
  `AssistantMessageEventStream` synchronously and forwards events with
  `for await (const event of source) target.push(event)` then
  `target.end(...)`. Proves: (a) `openAICompletionsApi().streamSimple` is the
  native `deps.streamSimple` to inject; (b) streams are async-iterable +
  push-based, so a wrapper can consume-and-forward or suppress events.
- `dist/api/openai-completions.js` (the real adapter):
  - `createClient(...)` passes `options?.fetch` straight into
    `new OpenAI({ ..., fetch, ... })` — the public per-request `fetch`
    override is the observation point. It sees every HTTP response the SDK
    sees, including non-2xx, WITHOUT reserializing prompts/tools.
  - `options?.onResponse` exists but fires only AFTER
    `client.chat.completions.create(...).withResponse()` resolves, i.e. only
    on success — a 503 throws `APIError` before `onResponse`. **Therefore the
    fetch wrapper, not `onResponse`, is the only reliable status/header
    observation point for the rejection.**
  - Ordering: `stream.push({ type: "start", ... })` happens only after the
    HTTP response is obtained successfully. A 503 attempt therefore emits
    exactly one event: `{ type: "error", reason, error: <AssistantMessage> }`.
    "No retry after any emitted stream event" is implementable by tracking
    whether any non-terminal event was seen this attempt.
  - Error path: `output.stopReason = options?.signal?.aborted ? "aborted" :
    "error"`; `output.errorMessage =
    formatProviderError(normalizeProviderError(error))`; pushes the single
    `error` event then `stream.end()`. Abort is thus visible as
    `reason === "aborted"`; we additionally honor the live signal ourselves.
  - `retryProviderRequest(..., { maxRetries: options?.maxRetries, ... })` may
    re-issue the HTTP request internally; our fetch wrapper simply overwrites
    its per-attempt capture with the latest response (see Risks R2).
  - `streamSimple` → `buildBaseOptions(...)` (in
    `dist/api/simple-options.js`) forwards `fetch`, `signal`, `timeoutMs`,
    `onResponse`, `maxRetries`, `headers` etc. into the inner `stream` call.
    Proves options passed to `deps.streamSimple` keep the fetch override.
- `dist/utils/event-stream.d.ts` + `dist/index.d.ts`:
  - `AssistantMessageEventStream` (constructor takes no args) with
    `push(event)`, `end(result?)`, async iteration.
  - `createAssistantMessageEventStream()` factory exists and
    `dist/index.d.ts` does `export * from "./utils/event-stream.ts"` — the
    ROOT `@earendil-works/pi-ai` export IS safe for the factory.
  - The root index does NOT re-export `./api/openai-completions.ts` — only
    `./api/lazy.ts` — confirming the contract's warning: the native adapter
    must be imported from the `api/openai-completions.lazy` subpath, not the
    root.

### 3.3 Extension registration (docs, both read completely)

- `docs/custom-provider.md`:
  - `ProviderConfig` includes
    `streamSimple?: (model, context, options?) => AssistantMessageEventStream`
    — "Custom streaming implementation for non-standard APIs" — registered
    via `pi.registerProvider("my-provider", { ..., streamSimple })`.
  - Override-existing-provider semantics: registering without `models` keeps
    all existing models; the same merge logic applies to a `streamSimple`-only
    registration for the EXISTING `zai` provider, so configured auth,
    endpoint, models and request options are preserved untouched.
  - `pi.unregisterProvider("zai")` "removes ... custom stream handler
    registrations. Any built-in models or provider behavior that were
    overridden are restored." — scoped to that one provider; other providers
    are never touched. This is the disable/restore path.
- `docs/extensions.md`: extension = default-export factory receiving
  `ExtensionAPI`; TypeScript loaded via jiti; directory layout
  `<dir>/index.ts` for auto-discovery; sibling `.mjs` import works (plain
  ESM, no build step).

## 4. Design

### 4.1 Strict eligibility gate (all must hold; ANY miss = passthrough)

Request-level gate, evaluated on the model object AND on the observed HTTP
exchange:

1. `model.provider === "zai"` and `model.id === "glm-5.3-flash"` (checked once
   per call; if false → return `deps.streamSimple(model, context, options)`
   directly — same function object, zero wrapping).
2. The fetch wrapper only classifies a response as a queue rejection when ALL
   of:
   - request URL parses to a loopback host (`127.0.0.0/8`, `::1`,
     `localhost`) with port `8766` and path exactly
     `/api/coding/paas/v4/chat/completions` — checked on the REQUEST url; a
     redirect that lands elsewhere fails this check on
     `response.url` too (never accept stamps from foreign origins/central);
   - `status === 503`;
   - `response.headers.get("x-anthropic-throttle-proxy") === "1"` AND
     `response.headers.get("x-anthropic-throttle-queue-timeout") === "1"`
     (case-insensitive via Headers);
   - body text `includes` the exact public sentence (tolerating the trailing
     newline only).
3. Terminal native error event qualifies for retry only if, additionally:
   - `reason === "error"` (never `aborted`) and parent signal not aborted;
   - no event of ANY type (terminal or non-terminal) was emitted earlier in
     this attempt;
   - `error.content.length === 0` and EVERY usage and cost field is zero
     (`usage.{input,output,cacheRead,cacheWrite,totalTokens}` present and 0,
     optional `cacheWrite1h`/`reasoning` 0 when present, `usage.cost.*` all
     0) — not just `usage.output`/`cost.total`;
   - the fetch wrapper captured a fully-gated rejection on this attempt.

Anything else (auth/quota errors, raw upstream 503 without markers, missing
marker, foreign origin, redirect, partial stream with content) is forwarded
UNTOUCHED: the exact native event object is re-pushed to our outer stream, no
copy, no rewrite.

### 4.2 Attempt loop

```
createQueueWaitStream(deps) → (model, context, options) => outer stream
  if !model-eligible: return deps.streamSimple(model, context, options)
  outer = deps.createEventStream()
  (async () => {
    rejections = 0; waitedMs = 0
    loop:
      if signal?.aborted → emit aborted error, done
      attempt = { rejection: null, sawContent: false }
      wrappedFetch = capture(url, init)   // §4.1 gate 2, via res.clone()
      inner = deps.streamSimple(model, context, { ...options, fetch: wrappedFetch })
      for await (event of inner):
        if event.type !== "error": attempt.sawContent = true; outer.push(event); continue
        if retryable(event, attempt, rejections, budget):
          rejections++
          delay = retryAfterMs(rejection) + positiveJitter()
          if waitedMs + delay > maxWaitMs → emit exhaustion error, done
          deps.onWait?.({attempt: rejections, delayMs, retryAfterMs, waitedMs})
          try { await deps.sleep(delay, signal) } catch { emit aborted error; done }
          waitedMs += delay; deps.onWait?.(null); goto loop
        outer.push(event); outer.end(); onWait(null); return   // passthrough, same object
      // 'done' event terminal: forwarded in the loop; stream ended natively
  })()
  return outer
```

- Sleep happens only AFTER the inner stream has fully ended (the 503 error
  event terminates it), so the native per-attempt HTTP timeout is already
  finished — we never sleep inside the SDK's fetch timeout or recreate an
  abort. Parent AbortSignal is passed INTO `sleep`, so abort during the wait
  rejects promptly (validated by "abort while sleeping").
- Abort race before fetch: the signal-aborted check at loop top plus the
  native SDK's own signal handling; no retry ever starts after abort.
- `deps.onWait(null)` fires on every exit path: success (done), passthrough
  error, retry-exhausted error, abort, budget exhaustion.
- Concurrency isolation: all state (`rejections`, `waitedMs`, `attempt`,
  `outer`) lives in the per-call closure. No module-level mutable state; the
  only module-level values are frozen constants. Concurrent requests cannot
  see each other.

### 4.3 Waiting math

- `Retry-After` parse: from the captured response headers; integer/float
  seconds → ms; else HTTP-date → `Date.parse(v) - now()`; invalid, absent, or
  `<= 0` → `FALLBACK_RETRY_AFTER_MS = 15_000` (conservative, non-hot: 3× the
  proxy's 5 s floor — a saturated lane re-entered too early just re-queues).
- Positive jitter: `jitterMs = Math.max(1, Math.ceil(deps.random() * JITTER_MAX_MS))`
  with `JITTER_MAX_MS = 1000`; `delay = retryAfterMs + jitterMs` — strictly
  positive even when `random() === 0` or Retry-After parses to 0.001 s.
- Budget: `maxWaitMs` (default 1_800_000) bounds the SUM of admission sleeps
  only — never an already-started successful stream (a `done` terminal is
  forwarded regardless of waited time). Before sleeping, `waitedMs + delay >
  maxWaitMs` → terminal exhaustion error.
- `maxRejections` (default 32) bounds the number of queue rejections that are
  each followed by exactly one re-dispatch; the `(maxRejections + 1)`-th
  eligible rejection → terminal give-up error (never the stamped 503).
- `deps.allowedBaseUrls` (AMENDED): DI-only test seam — exact `http://`
  loopback base URLs whose origin is additionally eligible; the completion
  path, method/status/stamps/body/no-redirect/empty-content/zero-usage/
  no-prior-event gates stay mandatory. Never wired from `index.ts`.

### 4.4 Terminal messages (exhaustion / abort)

Exhaustion text is explicit, self-describing, and MUST NOT contain generic
retry triggers (`503`, `rate limit`, `retry`, `429`):

- budget: `queue-wait: admission wait budget exhausted after N queue rejections and X ms of waiting; the model was never streamed`
- attempts: `queue-wait: gave up after N queue rejections; the model was never streamed`

Shaped exactly like a native error event (`stopReason: "error"`, empty
content, zero usage) so downstream Pi handling is indistinguishable from a
normal provider error. Abort: `stopReason: "aborted"`, message
`Request was aborted` (mirrors the native adapter).

### 4.5 What is deliberately NOT done

- No semaphore/queue/proxy change; no `Retry-After`-bearing 429 handling
  (native retry already covers pushback); no hiding of auth/quota/overflow/
  partial-stream errors; no logging of Authorization headers or request bodies
  (the fetch wrapper reads only status/headers/clone-text of the RESPONSE, and
  `onWait` payloads carry counters only); no vendor SDK; no new dependency.

## 5. Shared interface (frozen for the test-author worker)

```js
// queue-wait.mjs exports
export function createQueueWaitStream(deps)
export const QUEUE_WAIT_DEFAULTS   // { maxWaitMs: 1_800_000, maxRejections: 32,
                                   //  fallbackRetryAfterMs: 15_000, jitterMaxMs: 1_000 }
export const QUEUE_BODY_TEXT       // exact public proxy sentence
export const QUEUE_MARKER_HEADER / QUEUE_TIMEOUT_HEADER
export function parseRetryAfterMs(value, nowMs) // exported for tests
```

`deps` (required): `streamSimple` (native OpenAI-completions
`StreamFunction`), `createEventStream` (`() => AssistantMessageEventStream`).
Optional: `now` (ms, default `Date.now`), `sleep(ms, signal)` (default
setTimeout + abort rejection), `random` (default `Math.random`),
`onWait(info | null)` (default no-op), `maxWaitMs`, `maxRejections`,
`fallbackRetryAfterMs`, `jitterMaxMs`, `allowedBaseUrls` (DI-only test seam,
§4.3). Also exported: `QUEUE_BODY_FULL` (`QUEUE_BODY_TEXT + "\n"`),
`QUEUE_PROVIDER_ID`, `QUEUE_MODEL_ID`, `QUEUE_COMPLETIONS_PATH`,
`QUEUE_DEFAULT_PORT`.

`onWait(info)` payload: `{ attempt, delayMs, retryAfterMs, fallback,
waitedMs }` — counters only, never headers/bodies/tokens.

Return contract: `(model, context, options) => AssistantMessageEventStream`
identical in shape to `deps.streamSimple`'s return; ineligible calls return
`deps.streamSimple(...)` itself.

## 6. Proposed exact file changes

### 6.1 NEW `clients/pi-queue-wait/queue-wait.mjs`

Pure ESM, no imports. ~230 lines. Functions:

- `createQueueWaitStream(deps)` — resolves defaults, returns the stream fn.
- inner `runLoop()` — the §4.2 state machine.
- `captureFetch(innerFetch, signal, attempt)` — wraps `options.fetch ??
  globalThis.fetch`; clones 503 responses (`res.clone()`), reads the clone's
  text + headers into `attempt.rejection = { status, headers, bodyText,
  requestUrl, responseUrl }`, always returns the ORIGINAL `res` unmodified so
  the SDK path is byte-for-byte unchanged.
- `isLoopbackZaiLane(url)` — URL gate from §4.1(2).
- `rejectionMatches(rejection)` — the full stamp + body gate.
- `retryableError(event, attempt, rejections, waitedMs)` — §4.1(3) + budget.
- `parseRetryAfterMs(value, nowMs)`, `positiveJitter(random)`.
- `emitError(outer, model, options, stopReason, message)` — native-shaped
  error event (zero usage, empty content) + `end()`.

### 6.2 NEW `clients/pi-queue-wait/index.ts` (AMENDED — implemented as shown)

```ts
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import { createQueueWaitStream } from "./queue-wait.mjs";

export default function (pi: ExtensionAPI) {
  let ui; // captured from session_start / agent_start ctx
  const streamSimple = createQueueWaitStream({
    streamSimple: openAICompletionsApi().streamSimple,
    createEventStream: createAssistantMessageEventStream,
    onWait: (info) => { /* ctx.ui.setStatus("queue-wait", …) / undefined; try/catch */ },
    // allowedBaseUrls deliberately NOT set (DI-only test seam).
  });
  pi.registerProvider("zai", { api: "openai-completions", streamSimple });
  // NO commands (Q3): disabling is extension removal/reload only —
  // unregistering zai could erase another extension's merged overlay.
  // NO config export (Q2): knobs are constructor deps only.
}
```

- Only `"zai"` is ever registered — `api` + `streamSimple` ONLY (the gate
  proved `validateExtensionProvider` throws without `api`) → configured
  auth/endpoint/models/request options preserved; no other provider touched.
- The loader-safe `@earendil-works/pi-ai/compat` entry is imported because the
  jiti alias mangles the `.lazy` subpath (amendment #1); `compat` re-exports
  the same factory and `createAssistantMessageEventStream` (via `./index.js`).

### 6.3 NEW `clients/pi-queue-wait/README.md`

Sections: What/Why (spec 227, :8766 lane, stamped 503), Install
(`~/.pi/extensions/pi-queue-wait/`), Knobs table (`QUEUE_WAIT_DEFAULTS` +
constructor `deps` ONLY — no config/env surface in v1 per Q2), Safety
envelope (never-retried list), NO commands section (Q3: removal/reload only),
Observing (`onWait`), Relation to proxy (`x-anthropic-throttle-*`,
`Retry-After` default-configured bounds [5, `THROTTLE_QUEUE_RETRY_AFTER_MAX_S`
=300] s — a tunable cap, not an immutable universal one), Limitations.

Implemented: `clients/pi-queue-wait/{queue-wait.mjs,index.ts,README.md}`.
Not implemented (out of ownership): `queue-wait.test.mjs`, `native-replay.mjs`
(test-author worker).

### 6.4 EDIT `specs/227-pi-queue-wait/transport-plan.md`

This document; kept current with any plan-gate feedback.

## 7. Test design contract (interface the test-author worker builds on)

Owner: test-author worker (`queue-wait.test.mjs`, `native-replay.mjs`,
`test-plan.md`). The engine is dependency-injected, so:

- `node --test clients/pi-queue-wait/queue-wait.test.mjs` uses fake
  `deps.streamSimple` returning real `AssistantMessageEventStream`s
  (via injected `createEventStream`) and real `Response` objects from the
  fetch wrapper — no network.
- Required coverage (mirrors contract validators): stamped-503-then-success
  keeps the turn pending with no intermediate assistant error; every §4.1
  negative (wrong provider/model passthrough returning `deps.streamSimple`
  itself, auth/quota error, raw 503, missing either marker, foreign origin,
  redirect, partial stream with any prior event, non-empty content or nonzero
  usage on the error message) forwards the native event object unchanged;
  Retry-After seconds/HTTP-date/absent/invalid paths; strictly positive
  jitter; `maxWaitMs` and `maxRejections` ceilings produce the exhaustion
  text with no `503`/`rate limit`/`retry` substrings; abort before fetch,
  abort during sleep (signal stays live), no retry after abort; concurrent
  requests isolated; `onWait(null)` on every exit path; fetch wrapper returns
  the original Response object.
- `native-replay.mjs` drives the REAL Pi 0.84.4 adapter
  (`openAICompletionsApi().streamSimple`) against a loopback fake proxy to
  prove: unchanged request serialization, marked rejection → silent retry →
  success, and that native error events carry empty content/zero usage.

## 8. Risks and mitigations

- **R1 — `Response.clone()` body competition.** The SDK also reads the 503
  body to build its APIError. Mitigation: clone FIRST, read the clone
  asynchronously (never await before returning `res`); undici supports
  concurrent clone+read. If a race still surfaces, fall back to reading
  `res.headers`-only gating plus body check from the native error message
  (contract text is embedded there after `503 `) — noted, not chosen by
  default.
- **R2 — native internal retry (`retryProviderRequest`).** Several HTTP
  attempts can occur inside one `streamSimple` call; the capture holds only
  the latest response. Mitigation: gate on the latest capture AND on the
  native error event's zero-output shape; a stale capture cannot fabricate a
  rejection because the body text + both markers must match the SAME
  response.
- **R3 — lazy wrapper opacity.** `openAICompletionsApi().streamSimple`
  resolves the real module asynchronously inside `lazyStream`; setup failures
  produce zero-usage error events that superficially resemble candidates.
  Mitigation: the gate requires a captured stamped 503, which a setup failure
  cannot produce — such errors forward untouched.
- **R4 — `zai` lane path variance.** The gate hard-codes
  `/api/coding/paas/v4/chat/completions` per contract. If the operator's lane
  differs, the extension silently passes through (safe default); README
  documents the exact expected URL so a mismatch is diagnosable.
- **R5 — jiti/`.mjs` interop.** `index.ts` is loaded via jiti; importing a
  sibling `.mjs` is plain ESM and supported. Fallback (if the plan gate
  prefers): inline the engine into `index.ts` — rejected for now because the
  contract fixes `queue-wait.mjs` as the shared module.
- **R6 — event object identity.** Forwarding the SAME native event object
  (not a clone) keeps passthrough byte-for-byte; only suppressed retry
  errors are never re-pushed.

## 9. Open questions for the plan gate — RESOLVED

1. `FALLBACK_RETRY_AFTER_MS = 15_000` — ACCEPTED (amendment #13).
2. Config export — REJECTED (Q2): deps-only in v1.
3. Commands — REJECTED (Q3): removal/reload only; no `queue-wait-off/on`.
