# Public Pi admission-wait extension — worker contract

## Goal

A temporary, locally stamped queue rejection must keep the current Pi model
request pending and retry after its advised delay, rather than lose the turn.
Do not change the proxy semaphore or hide real quota/auth/partial-stream errors.
Implement as a standalone PUBLIC Pi extension/client integration here, using
Pi's documented provider registration. No private infrastructure source is
needed. No vendor SDK replacement and no new runtime dependency.

## Ownership and phase gate

Two GLM-5.3-Flash/max implementation workers, separate worktrees:

1. Transport worker owns `clients/pi-queue-wait/queue-wait.mjs`, `index.ts`,
   `README.md` and `specs/227-pi-queue-wait/transport-plan.md`.
2. Test-author worker owns `clients/pi-queue-wait/queue-wait.test.mjs`,
   `native-replay.mjs` and `specs/227-pi-queue-wait/test-plan.md`.

FIRST produce the assigned PLAN only, then stop. The controller obtains a
capable different-family plan check and sends explicit PLAN APPROVED before
implementation. Workers do not judge, review, merge, deploy, or claim done.
No edits outside the named public files. Do not commit/push. At most two repair
rounds. The container has a 30-minute lifetime; no self-spawning or paid probes.
The parent executes validators; these workers have file tools, NOT bash/network
or delegation tools. Preserve the safety boundary rather than requesting more.

## Shared module interface

`queue-wait.mjs` exports `createQueueWaitStream(deps)` returning the usual Pi
`(model, context, options) => AssistantMessageEventStream` function.

`deps.streamSimple` is the native OpenAI-completions stream implementation;
`deps.createEventStream` is Pi's native stream factory. Optional dependencies:
`now` (milliseconds), `sleep(ms, signal)`, `random` (0..1), `onWait(info|null)`,
`maxWaitMs` (default 1_800_000), `maxRejections` (default 32). Keep the interface
small; propose an evidence-backed adjustment in the plan if needed.

The extension `index.ts` registers a stream override for the EXISTING `zai`
provider only. Preserve configured auth, endpoint, models and request options.
Use the native adapter and the public per-request `fetch` option to observe
status/headers without reserializing prompts/tools. The native adapter's public
subpath in Pi 0.84.4 is `@earendil-works/pi-ai/api/openai-completions.lazy`.
Do not assume the root export documented in custom-provider.md actually exists.

## Strict eligibility

- Provider `zai`, model `glm-5.3-flash`, POST to loopback HTTP port 8766,
  path `/api/coding/paas/v4/chat/completions`; never foreign redirects/central.
- HTTP 503 AND `x-anthropic-throttle-proxy: 1` AND
  `x-anthropic-throttle-queue-timeout: 1` observed on that response.
- Exact public proxy text: `proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue` (trailing newline in HTTP body; native SDK adds `503 `).
- Native error must have empty content and zero usage/cost. No retry after ANY
  emitted stream event or partial output/tool-call. Inspect native stream
  ordering; do not copy a model's claim of zero output into a verdict.
- Preserve every unrelated response/error byte-for-byte through the native
  path. Never log bearer tokens or request bodies. Cancellation sends no retry.

## Waiting

Honour Retry-After (seconds or HTTP date) plus POSITIVE jitter. Missing/invalid
Retry-After requires a conservative non-hot fallback. The overall budget limits
admission waits only, not an already-started successful stream. Native HTTP
per-attempt timeouts must END before sleeping (do not sleep inside the native
SDK fetch timeout and recreate the abort). The parent AbortSignal remains live
through sleep. Explicit non-retryable exhaustion text must not contain generic
retry triggers such as `503`/`rate limit`. Clear waiting UI on every exit path.
Retry the same model request, not tools, prompts, whole sessions, or commands.

## Validators (parent executes)

- `node --test clients/pi-queue-wait/queue-wait.test.mjs`
- Native Pi 0.84.4 adapter against fake loopback proxy: marked rejection then
  success, no intermediate assistant error, unchanged request serialization.
- Abort while sleeping, abort race before fetch, budget/attempt ceilings,
  positive jitter and Retry-After floor, concurrent request isolation.
- Auth/quota/raw 503/missing marker/foreign origin/redirect/partial-stream
  failures must never be retried. Wrong provider/model must be a passthrough.
- Actual extension load/registration must be exercised, not just pure helper.
  Test provider model/auth/endpoint preservation; extension disabling/restoring
  should not clobber another provider. No live inference in validators.
- No weakening existing proxy tests, hooks, CI thresholds or endpoint policy.

## Public reference roots

Repository files are public; especially `src/anthropic_throttle_proxy/proxy.py`
(queue timeout response), `config.py` (stamps), and related tests.
Pi source/docs are mounted read-only at `/opt/pi/@earendil-works/pi-coding-agent`.
Read applicable Pi .md docs completely and follow relevant API cross-references.
No access to private host home directories, vault, production data, credentials,
other projects, or private infrastructure repositories is part of this task.
