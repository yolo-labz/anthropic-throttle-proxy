# Acceptance — MiMo Pi queue wait (243)

23/09/2026, desktop. **Source acceptance only; not installed or activated.**
Generator: OpenAI/GPT. ZAI quota-blocked; no ZAI delegation or inference probe.

## Causality

Hypothesis: main's ZAI-only provider/path gate leaves MiMo's local queue rejection
to ordinary Pi retries, instead of the existing bounded admission-wait engine.
The journal sequence in [plan.md](plan.md) independently matches the screenshot:
2/4/8/16-second retries while the proxy advised 210–241 seconds.

Alternative explanations checked: native-adapter drift (main's existing 119
cases pass on Pi 0.85.1); malformed queue evidence (exact source envelope and
actual adapter's one empty-content, all-zero-usage error verified). This does not
prove upstream quota/concurrency health or that already-running tabs are fixed.

## RED → GREEN

Actual package: `@earendil-works/pi-coding-agent` **0.85.1**, with its own
`@earendil-works/pi-ai/compat` native OpenAI-completions adapter, real extension
loader and real `ModelRuntime`. No SDK/loader replicas in the MiMo native tests.

```sh
export PI_CODING_AGENT_ROOT=/home/notroot/.cache/pi-npx/0.85.1/_npx/a54d9a87e5358117/node_modules/@earendil-works/pi-coding-agent
node --test clients/pi-queue-wait/mimo-replay.test.mjs
node --test --test-concurrency="$(nproc)" clients/pi-queue-wait/*.test.mjs
```

- Main `7ddf462`, old suite: **119 passed**.
- MiMo tests before production edits: **36 passed / 55 failed**, exit 1;
  [red.tap](red.tap). Positive cases record `[]` instead of `[210001,241001]`.
  Enabled native retries also escape expected attempt counts on small-delay
  guard fixtures, because the entire MiMo request bypasses the wrapper.
- Additional absence-of-configuration regression: unconditional MiMo overlay
  created an empty provider/auth entry (40 → 41 providers). **RED 1 failed**,
  [registration-red.tap](registration-red.tap). Registration now occurs at
  session start only when that provider already exists.
- Final combined suite: **216 passed / 0 failed / 0 cancelled / 0 skipped**,
  [green.tap](green.tap). The existing 119 ZAI/unit tests remain included.

The HTTP fixture runs on an ephemeral loopback port. Caller `fetch` maps the
logical production URL to that server and preserves the logical final URL.
Thus production's strict 8773/path gate is exercised **without** opening the
live service or using the DI origin allowlist. 210/241-second sleeps are
recorded rather than elapsed; cancellation separately exercises the real timer.
Fault-injection usage/prior-event tests explicitly alter the actual adapter's
terminal event; genuine partial text/thinking/tool streams use actual SSE.

### Coverage

- `mimo-desktop` models `mimo-v2.6`, `mimo-v2.6-pro`, `mimo-v2.6-flash`;
  `maxRetries=0/2`; 210001 + 241001 ms exactly fills the admitted sleep budget.
- First-wait and cumulative budget refusal; rejection ceiling with native
  `maxRetries=3`; synthetic terminal errors contain no hot-retry triggers.
- Both stamps exactly `1`, exact body/newline/status, provider/model, http,
  host/port/path, credentials/query/fragment, redirect and final URL guards.
  Canonical localhost/IPv4/IPv6; MiMo↔ZAI crossed tuples rejected.
- Real zero-usage/no-output native envelope; every usage/cost scalar with
  nonzero/null/undefined/string fails closed; content/reason/stopReason drift;
  prior start event vetoes a fully proved rejection. Native SSE partial text,
  thinking and tool calls never replay.
- Cancellation before dispatch and during real admission sleep; no redispatch.
- Unrelated 429/raw503/wrong body retain caller-enabled native retry behavior.
- Original response headers unmodified. All caller options except the observing
  fetch reference preserve identity/value; model/context retain identity;
  method/path/headers/body match native serialization across retries.
- Real loader/session-start/ModelRuntime preserves configured MiMo catalog,
  endpoint and resolved auth/headers; actual runtime stream dispatch reaches the
  overlay and emits/clears wait status. Absent MiMo creates nothing. Existing
  ZAI catalog/auth/unregister checks remain intact.

## Repository checks

Using the existing development environment (no installation):

```sh
PYTHONPATH="$PWD/src" ../anthropic-throttle-proxy/.venv/bin/python -m pytest -q
../anthropic-throttle-proxy/.venv/bin/ruff check src tests
../anthropic-throttle-proxy/.venv/bin/ruff format --check src tests
node --check clients/pi-queue-wait/queue-wait.mjs
node --check clients/pi-queue-wait/mimo-replay.test.mjs
git diff --check
```

**1,337 Python tests passed**, 125 existing warnings, 78.54 s:
[python-tests.txt](python-tests.txt). Ruff: all checks passed, 74 files already
formatted; node syntax and diff checks exit 0: [static-checks.txt](static-checks.txt).

## Boundary / rollback

No installation, extension reload, service restart, deployment, live inference,
Nix pin, authentication/catalog/config mutation or other-pane interaction.
Dirty 227 worktree and coordinator Notes-1816-rate-limit-fleet remain untouched.
Source landing is reversible with one revert PR of its squash commit. Activation
and verification of already-running tabs require a separately authorized slice.
