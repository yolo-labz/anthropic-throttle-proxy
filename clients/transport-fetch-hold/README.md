# 308 — transport fetch-hold (source rehearsal)

Source-only prototype of ONE reusable transport capability: a **fetch hold**
wrapped around the supported `options.fetch` seam that
`clients`' anthropic-messages family passes into `createClient(model, apiKey,
headers, options.fetch, …)` (pi-ai `anthropic-messages.js:379`), whose
internal retry (`retryProviderRequest` around `client.beta.messages.create`
with SDK `maxRetries: 0`, `:393`) re-enters the same fetch.

## What it is

`hold-fetch.mjs` — dependency-free wrapper factory over ANY fetch:

- **hold()** closes the gate BEFORE an internal retry: new calls produce zero
  wire sends; a **slow body** (stream / async iterable) is **closed before any
  upload** rather than parked; a buffered body parks until release.
- **resume()** is the only opener (idempotent; no-op with nothing parked).
- **cancellation** under hold releases the parked caller promptly.
- an **admitted** response — including SSE — passes through unchanged.

`rehearsal.mjs` — loopback verification against the **installed** `@anthropic-ai/sdk`
(discovery is read-only; `ANTHROPIC_SDK_PATH` overrides), synthetic key and
synthetic payloads, **zero real provider traffic**. Eight checks:

| # | Check |
|---|-------|
| 1 | first attempt allowed (reaches the wire) |
| 2 | gate closes BEFORE the internal retry (retry parks at the seam) |
| 3 | zero new wire sends until resume |
| 4 | slow body closed before any upload |
| 5 | cancellation under hold releases |
| 6 | admitted SSE preserved byte-exact (gate closes mid-stream) |
| 7 | negative control of resume (resume is the only opener; exactly one release) |
| 8 | failure exits code ≠ 0 (`--selftest-fail` demonstrates) |

```sh
node clients/transport-fetch-hold/rehearsal.mjs            # exit 0 = all pass
node clients/transport-fetch-hold/rehearsal.mjs --selftest-fail   # exit 1
```

## What it is NOT (explicit)

- It does NOT reimplement retry, SSE or streaming — the built-in transport
  keeps owning them; this wrapper only gates new sends at the fetch boundary.
- It changes NO provider, model or billing ids; it uses no native provider
  registry and loads no live extension.
- A source rehearsal **authorizes no live gate, no client rollout and no
  24h claim**. Live enablement is separate, authorized work.
