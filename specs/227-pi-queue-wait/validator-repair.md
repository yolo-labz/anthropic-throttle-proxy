# Validator fidelity repair — 14/09/2026

## Provenance and status

OpenAI GPT-5.6 Sol authored this validator-only repair after the bounded GLM attempt produced no edits. This is generator evidence, not independent approval or a release verdict. The transport remains unchanged; the parent owns the subsequent transport repair and independent gate.

## Controller rerun (supersedes sandbox-only results below)

On desktop, the required `node --test` command with `PI_CODING_AGENT_ROOT`
set to the installed Pi 0.84.4 root completed: **76 tests, 61 passed, 15 failed,
0 cancelled**. All six native adapter/loader cases passed, including exact
serialization and preserved provider configuration; the 15 failures are the
transport-facing cases listed below. The transport SHA256 is unchanged:
`2840985c021a33cdc9078334644cf01e6126e135473b0a1a0579bdd4b16b56de`.

The first controller run found one additional fixture mismatch: the real native
error RETAINS the trailing newline. Removed fixture/expectation trimming rather
than modifying native output. Both `.mjs` files pass `node --check`. Evidence
is retained in fleet-coordination's queue-swarm `controller-final-validator-20260914.log`.
The Codex generator hit its 15-minute cap after writing the repairs/report;
controller execution, not its incomplete final response, established these results.

## Fidelity corrections applied

- Native assistant error fixtures now carry the real `role`, scalar usage fields, and nested `usage.cost` fields. Each required field has a negative test that reaches the transport.
- Native status and text assertions require exact passthrough, including the real status code and unmodified response body (trailing newline preserved).
- The Pi compatibility loader resolves the installed package from `@earendil-works/pi-ai/package.json` when that subpath is exported. Pi 0.84.4 does not export that subpath, so the fixture falls back to Node's package search paths, validates the discovered manifest, and follows its `exports["./compat"].import` target. It never resolves the import-only compatibility module with `require.resolve()`.
- Registration validation compares registry state before and after registration, preserves provider model/auth/endpoint state, and verifies exact restoration after unregistering.
- The native replay model includes all required rates. Plain and wrapped requests use the same fake server and must produce deeply equal serialized requests.
- Deterministic regressions cover stale internal-fetch clones, strict full URLs, numeric HTTP-date timing, abort during a stalled clone, non-abort sleep rejection, and synchronous stream/iterator throws.
- Every gate and fake server is bounded and cleaned up in `finally`; no native case is skipped.

## Test evidence

Required command, using the public Pi package root:

```text
timeout 60s env PI_CODING_AGENT_ROOT=/home/notroot/.cache/pi-npx/0.84.4/_npx/1f276a68aabfc75c/node_modules/@earendil-works/pi-coding-agent node --test clients/pi-queue-wait/queue-wait.test.mjs
```

In the Codex workspace sandbox's Node 22.23.2, default test-file isolation reports only the file-level aggregate for the failing child process: **1 test, 0 passed, 1 failed**, exit status 1. Direct execution of the same `node:test` registrations exposes the individual cases: **76 tests, 56 passed, 20 failed, 0 skipped, 0 cancelled, 0 todo**. The suite is red against the unmodified transport.

The isolated unit selection reports **70 tests, 55 passed, 15 failed, 0 skipped**. These transport-facing failures are:

1. `unit: strict full URL — request URL has a query`
2. `unit: strict full URL — request URL has credentials`
3. `unit: strict full URL — request URL has a fragment`
4. `unit: strict full URL — final URL differing only in query is ineligible`
5. `unit: Retry-After as HTTP-date resolves against the injected clock`
6. `unit: missing usage.cost.input fails closed`
7. `unit: missing usage.cost.output fails closed`
8. `unit: missing usage.cost.cacheRead fails closed`
9. `unit: missing usage.cost.cacheWrite fails closed`
10. `unit: missing usage.cost.total fails closed`
11. `unit: non-abort sleep rejection preserves the failure as one native error`
12. `unit: synchronous streamSimple throw emits exactly one native terminal error`
13. `unit: async iterator throw emits exactly one native terminal error`
14. `unit: stale clone from an earlier internal fetch cannot authorize outer redispatch`
15. `unit: abort during a stalled clone terminates promptly without retry`

The real Pi loader/registration selection reports **1 test, 1 passed**:

- `extension: real Pi loader captures one zai registration; ModelRuntime preserves configured providers`

## Historical generator sandbox boundary (not desktop execution)

Loopback binding is denied in this workspace sandbox with `listen EPERM: operation not permitted 127.0.0.1`. The five native cases remain registered and fail visibly rather than being skipped:

1. `native: marked queue rejection then success — no intermediate assistant error`
2. `native: request serialization identical with and without the wrapper`
3. `native: real adapter's 503 is one terminal error event, empty content, zero usage`
4. `native: wrong provider is a passthrough even against an eligible-looking 503`
5. `native: abort while parked in an admission wait`

The direct full run therefore has 15 expected transport failures plus 5 environment-blocked native failures. Native behavior still requires execution in a workspace that permits bounded loopback listeners; this evidence does not independently approve the validators or declare the feature done.
