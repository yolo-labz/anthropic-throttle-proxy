# Shared OpenAI API token admission

Direct Pi `openai-api/gpt-6.2-astra` requests use the native Responses transport
and `https://api.openai.com/v1/responses`. The subscription continuity helper
does not wrap that provider. Per-process backoff therefore cannot reserve a
shared organization's remaining tokens for concurrent workers.

`admission.mjs` adds admission at the native `options.fetch` boundary. Separate
processes use the same private SQLite database and explicit organization/rate
group. A transaction records a conservative token reservation before each send.
The SQLite lock is released while waiting. In-flight reservations do not expire.
Headers or a transport error start a full retention window; a dead worker
retains uncertain debt for a full window from discovery, including PID reuse.
Successes and failures never refund dispatched debt. A restart retains it. Initialization and response observation retry writer
contention without retransmitting an already received response. After dispatch,
durable settlement completes before cancellation returns.

An explicit `rate_limit_exceeded` HTTP 429 records a shared not-before deadline.
It honors the longest Retry-After, retry-after-ms, token-reset or body delay,
with a one-second minimum and no upper truncation. The same buffered request
is retried only after acquiring the shared budget again. The SDK continues to
own SSE, tool results and terminal parsing. Unknown 429s, insufficient quota,
authentication errors, disconnects and incomplete streams are never replayed.

## Qualification

Requires Linux and Node 22.13 or newer with built-in SQLite and TypeScript
stripping. Worker ownership uses Linux process start times to detect PID reuse.

```sh
node --experimental-strip-types clients/openai-token-admission/admission.test.mjs
PI_AI_OPENAI_RESPONSES="$HOME/.local/share/pi-fleet/current/node_modules/@earendil-works/pi-ai/dist/api/openai-responses.js" \
  node --experimental-strip-types clients/openai-token-admission/native.qualification.mjs
```

The first command runs 28 source tests. The second runs all 31, including
the installed native Responses provider through the provider wrapper. Child
processes use a synthetic key,
an empty HOME and an IPC upstream; their global network fetch throws. No live
provider request or production load test is part of either command.

The concurrency fixtures cover 4M capacity with 3.95M already used and two
106,848-token requests, six processes sharing a rolling budget, and concurrent
739/904 ms rate rejections. Fixtures shorten the minute for the budget tests;
the Pi extension accepts only a 60,000 ms production window. Removing admission
with `OPENAI_ADMISSION_BASELINE=1` makes the same capacity oracle fail.

## Pi integration

`pi-extension.ts` wraps the existing direct API provider after the fleet parity
extension. It never calls `setModel`, edits authentication, changes effort or
touches subscription pools. Load it last with Pi's existing `--extension`
argument and set `PI_OPENAI_TOKEN_ADMISSION_CONFIG` to an owner-only JSON file.
Provision the following fields with the actual shared organization/rate group
and an absolute private database path:

| Field | Value |
| --- | --- |
| `schema` | `1` |
| `scope` | Explicit organization/rate-group identifier, identical across workers |
| `path` | Absolute SQLite path inside an owner-only directory on this host |
| `limit` | `4000000`, or a lower qualified org TPM limit |
| `windowMs` | `60000` |
| `models` | `["gpt-6.2-astra"]` |

The configuration contains no credential. Missing or invalid configuration
blocks this exact API model before startup can continue unguarded. Existing
requests and processes are unaffected by publishing these files. Deployment
requires loading the extension in the affected API workers at a safe boundary;
this source qualification does not claim it is already active fleet-wide.

## Bounds

The input bound is UTF-8 request bytes, including tools and history, plus the
output cap. It is deliberately conservative and is not tokenizer calibration.
Remote images, files, server-side conversation references and hosted tools
refuse before sending because their token cost is absent from those bytes.
Provider token observations are pessimistic high-water marks for one local
window, plus dispatched local debt retained after headers. This can wait longer than a short upstream
retry hint. A success cannot erase another worker's hold.

All covered workers on one host must share the same database and org/rate group.
This cannot govern uninstrumented clients or workers on another host, and it
does not guarantee zero provider 429s. Different limits for the same scope
refuse rather than silently split the bucket. Metadata contains hashed scopes,
timestamps and token bounds, never request bodies or credentials. The source
pytest gate requires Node with SQLite support. Installed-core qualification is
a separate explicit command and never silently skipped.
