# Native transport contract — Pi 0.99.1 fetch-level hold support (04/10 14:3x BRT)

Read-only PUBLIC source trace for the LoopConductor authority contract
(`home:w2N:p5` owns authority/loader — untouched; no prompt, no WIP overwrite).
No client-gate source edits, no runtime rollout. Producer classes/coverage
markers only — no account ids, bindings, prompts or credentials.

Source root: `~/.cache/pi-npx/0.99.1/_npx/78709cf4b2f1011c/node_modules/
@earendil-works/pi-coding-agent` (SDK transport in nested
`node_modules/@earendil-works/pi-ai`).

## Exact paths / call graph

| Piece | Path | Wiring |
|---|---|---|
| Agent transport entry | `dist/core/sdk.js` (~230-255) | `Agent.streamFn = (model, context, options) => { …; return modelRuntime.streamSimple(model, context, buildRequestOptions(model, options)) }` |
| `before_provider_request` | `dist/core/sdk.js:215` → `transformProviderPayload` → `runner.emitBeforeProviderRequest(payload)` | bound as **`Agent.onPayload`** — runs **once per Agent stream call, before the provider transport**, i.e. OUTSIDE `retryProviderRequest`. Errors are caught/continued (**fail-open**) |
| `before_provider_headers` | `dist/core/sdk.js:193-197` → `requestOptions.transformHeaders` = `emitBeforeProviderHeaders` | rides request options into the provider call; per root audit runs before SDK retry (once per provider call). Errors caught (**fail-open**) |
| SDK retry wrapper | `pi-ai/dist/utils/provider-retry.js:75` `retryProviderRequest(request, options)` — `for(;;)` loop, interruptible backoff, re-invokes `request()` per attempt | `pi-ai/dist/api/anthropic-messages.js:391-394`: Anthropic client built with `maxRetries: 0`; `retryProviderRequest(() => client.beta.messages.create(params, requestOptions).asResponse(), { maxRetries: options?.maxRetries, … })` |
| Provider registration | `docs/custom-provider.md`: `pi.registerProvider()` (async factory awaited at startup; also immediate after load), `pi.unregisterProvider()` restores built-in | **replacement semantics for the provider id** |
| Virtual models | `docs/virtual-models.md`: `pi.registerVirtualModel` / `modelRuntime.registerVirtualModel` | model mapping only — no transport control |
| Reload boundary | `pi.registerProvider` post-load calls take effect immediately; registration follows the documented queue/reload rules; `examples/extensions/reload-runtime.ts` | accepted reload boundary = the extension reload mechanism owned by p5 |

## Verdict: can supported registration cover EVERY MiMo physical retry?

- **Hooks alone: NO.** `before_provider_request`/`before_provider_headers` are
  error-caught (fail-open) and run outside `retryProviderRequest`; a throwing
  hook cannot refuse, and an await-hold at `onPayload` gates only the FIRST
  attempt of each Agent call — SDK retries inside `retryProviderRequest`
  re-invoke the transport callback without re-running `onPayload`.
- **`retryProviderRequest` is the only per-attempt envelope** (its `request()`
  callback is re-invoked per physical attempt) and exposes NO supported hook
  inside that callback.
- **Registration can, at the provider-implementation layer:** a
  `pi.registerProvider()` wrapper **with the same provider id** can put the
  hold at the top of its own transport function and drive its own per-attempt
  loop (or call `retryProviderRequest` itself), covering every physical
  attempt. Trade-off honestly stated: registration REPLACES built-in transport
  behavior for that id; model/provider/billing IDENTIFIERS and config stay
  unchanged (`unregisterProvider` restores built-in), but the transport
  implementation is no longer the built-in one. If "no provider changes" is
  read as forbidding implementation replacement, then **no supported interface
  exists** — the enforcement must stay provider-side (the proxy's local
  admission gate, which already covers all producers).
- **Header placement RESOLVED (independent verification 14:38):**
  `Models.applyAuth` (438-444) AWAITS `transformHeaders` and then STRIPS it
  before `provider.stream` — headers are conclusively **once-per-provider
  call, outside physical retry** (falsifier #5 settled: the per-attempt
  counter would show 1 per provider call).
- **THE supported seam for a per-attempt hold is `options.fetch`:**
  `anthropic-messages.js:379` passes `options.fetch` into `createClient`;
  `:393` retries `client.beta.messages.create(...)` with SDK `maxRetries: 0`
  under `retryProviderRequest`. An enforcing fetch-level wrapper rides this
  seam and gates EVERY physical attempt WITHOUT reimplementing retry/SSE and
  without changing provider/model/billing IDs — preferred over provider
  registration.

## Producer classes / coverage markers (enumeration only)

| Class | Path | Covered by onPayload hold? |
|---|---|---|
| P1 main generation (real clients) | `Agent.streamFn` → `streamSimple` | first attempt only |
| P2 SDK-retry physical attempts | `retryProviderRequest` iterations | **no** (outside hook) |
| P3 cache-warm requests | `cacheWarmer.start({model, context, options})` — real transport calls | first attempt only |
| P4 compaction/summary streams | own routing ids, same `streamFn` | first attempt only |
| P5 subagent/task streams | per-agent `streamFn` instances | first attempt only |
| P6 extension-redirected/virtual-model streams | `modelRuntime` mapping | same as owner class |

Every class converges on `modelRuntime.streamSimple` → provider transport →
`retryProviderRequest`; the per-attempt gap is common to all.

## Executable falsifiers

1. **SDK-retry closure:** a request failing retriable ×2 then succeeding must
   invoke the gate before EACH of the 3 physical attempts. Hooks fail this
   today (1 gate call per provider call) — this is THE closure test.
2. **Persistent hold:** gate never opens → ZERO physical attempts for the whole
   call, including retries (hooks: first attempt held, retries would escape if
   the first had been allowed).
3. **Slow-upload:** a held attempt must gate before ANY body byte is written;
   assert no partial upload begins while the gate is closed (large-body
   request, gate opens at t+δ).
4. **Cancel:** (a) abort while held → no physical attempt, no leaked timer;
   (b) abort mid-fetch after gate release → the attempt is counted as physical
   but cancellation propagates cleanly (no false closure claim).
5. **Header-placement probe — SETTLED** (14:38): `transformHeaders` is
   once-per-provider-call (`Models.applyAuth` awaits then strips before
   `provider.stream`); the counter would read 1 per provider call. The fetch
   wrapper seam is the correct per-attempt locus.
6. **Warm/compaction parity:** P3/P4 requests go through the same gate as P1.

## Accepted reload boundary

Extension-driven registration via the documented reload mechanism
(`examples/extensions/reload-runtime.ts`; post-load `registerProvider` takes
effect immediately) — owned by p5's loader/authority contract. No runtime
rollout from this seat.
