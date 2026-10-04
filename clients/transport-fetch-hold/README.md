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
- **RE-LOOP after every park** (310 defect 1): a parked call woken by
  `resume()` re-checks the gate before touching the wire, so
  `resume(); hold()` in the same turn keeps it parked — zero sends.
- **Abort-listener hygiene** (310 defect 3): the signal listener is removed on
  EVERY settle path — released or cancelled.
- **resume()** is the only opener (idempotent; no-op with nothing parked).
- **cancellation** under hold releases the parked caller promptly.
- an **admitted** response — including SSE — passes through unchanged.

`rehearsal.mjs` — loopback verification against the **installed** `@anthropic-ai/sdk`
(discovery is read-only; `ANTHROPIC_SDK_PATH` overrides), synthetic credential
and synthetic payloads, **zero real provider traffic**. The physical retry is
the **NATIVE `retryProviderRequest`** from the path-nested pi-ai contract
(`dist/utils/provider-retry.js`) driving
`client.beta.messages.create(…, { maxRetries: 0 }).asResponse()` in ONE call —
the real second dispatch inside the same native retry (310 defect 2: the old
rehearsal's two named calls were not a physical retry). Checks:

| # | Check |
|---|-------|
| 1 | first attempt allowed (native physical-retry call, wire send #1) |
| 2 | gate closes BEFORE the native retry fires (retry parks at the seam) |
| 3 | zero second wire dispatch until resume |
| 4 | slow body closed before any upload |
| 5 | cancellation under hold releases |
| 6 | admitted SSE preserved byte-exact (gate closes mid-stream) |
| 7 | resume completes the native retry (negative control) |
| 8 | regression: resume/immediate-hold race stays closed |
| 9 | regression: abort listener cleaned in held and resumed waiters |
| — | failure exits code ≠ 0 — proved by a SUBSTANTIVE check over real
    machinery (no empty forced-throw) via `--selftest-fail` |

```sh
node clients/transport-fetch-hold/rehearsal.mjs            # exit 0 = all pass
node clients/transport-fetch-hold/rehearsal.mjs --selftest-fail   # exit 1
```

## MiMo stream rehearsal (311) — the REAL OpenAI-compatible path

`mimo-rehearsal.mjs` — hypothesis test: the accepted fetch-hold gates the
REAL provider retry of the **OpenAI-compatible stream path** (the actual MiMo
route) and preserves terminal SSE / tool / usage / cancel semantics. The 310
rehearsal exercised the **Anthropic SDK only** — stream-path acceptance is NOT
inferred from a shared helper. This run invokes the **native installed**
openai-completions provider (`pi-ai/dist/api/openai-completions.js` —
`options.fetch` seam at `:186`, native `retryProviderRequest` around
`client.chat.completions.create(...).withResponse()` at `:195-197`) with local
synthetic model/context/key and the REAL `options.fetch`; `hold-fetch.mjs` is
reused **unchanged**. Lineage: 308 (prototype) → 310 (race/retry/leak fixes) →
311 (MiMo stream path).

| # | Check |
|---|-------|
| 1 | first 500 → gate held BEFORE the native 2nd attempt of the SAME call → zero wire until resume → `toolUse` terminal with valid tool + usage |
| 2 | already-admitted stream preserves bytes + terminal under closure (identical to open-gate baseline) |
| 3a | cancellation under hold does not invent completion (`aborted` error terminal) |
| 3b | slow upload closed before upload — zero bytes, no completion possible |
| 3c | uncertain send (every attempt dies mid-stream) does not invent completion — honest `error` |
| — | substantive falsifier with exit nonzero (`--selftest-fail`, real terminal observed under an inverted expectation) |

```sh
node clients/transport-fetch-hold/mimo-rehearsal.mjs            # exit 0
node clients/transport-fetch-hold/mimo-rehearsal.mjs --selftest-fail   # exit 1
```

Xiaomi quota/window semantics are UNTOUCHED: this is transport-level only.
The hold gates sends; it never reads or writes quota, windows or meters, and
every unknown (unmapped seats, unreadable windows) stays unknown.

## What it is NOT (explicit)

- It does NOT reimplement retry, SSE or streaming — the built-in transport
  keeps owning them; this wrapper only gates new sends at the fetch boundary.
- It changes NO provider, model or billing ids; it uses no native provider
  registry and loads no live extension.
- A source rehearsal **authorizes no live gate, no client rollout and no
  24h claim**. Live enablement is separate, authorized work. Source facts are
  retained; **mechanism acceptance stays HELD** until the 310 regressions
  (race, native physical retry, listener cleanup) are reviewed and pass on
  the exact head.
