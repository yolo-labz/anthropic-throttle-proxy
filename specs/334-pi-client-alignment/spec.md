# Spec 334 — pi client alignment against the throttle-proxy fail-fast 503

Status: PROPOSAL (spec + patch proposal only — no fleet-wide rollout, no `src/` edits, no commit)
Slice: pi client alignment (research + proposal)
Author: pi seat, 19/10/2026
Scope: how the **pi client** behaves when the free-gate proxy answers a queued
generate with its fail-fast 503, and the minimal client-side change that stops the
retry burn-loop **without** touching proxy `src/` and **without** a fleet rollout.

---

## 1. Incident ground truth (given)

Live incident 19/10 ~18:57–19:06 on the **go gate (:8770)**:

1. `budget-pushback` 429 → `aimd-shrink` pinned the shared bearer to
   `max_concurrent=1`.
2. With 171–303 s generations, every *other* request was refused **before it
   waited** — a `pre-queue-depth` 503 with `est_wait 210–355 s > max_wait 180 s`.
3. pi panes entered **retry loops** ("pi panes died in retry loops") and the
   user-visible symptom was a ~4×15 s loop then a stall.

Already-live **proxy**-side mitigations (other slice, NOT this one):
`THROTTLE_MAX_HOLD_RETRY_AFTER_S=240` on all4 gates; `THROTTLE_AIMD_MIN=2` on
the go gate. Client contract: pi idle timeout 300 s, `queue_max_wait` 180 s,
hold 240 s.

This slice addresses the **client** half: why pi retries a 503 that the proxy
explicitly refuses *in order to* keep the client out of the fair queue.

---

## 2. What the proxy actually sends (receipt)

`src/anthropic_throttle_proxy/proxy.py`, `_queue_wait_timeout_response`
(lines 778–830) builds the fail-fast 503:

- `status=503`
- header **`retry-after: <retry_after_s>`** — an integer **in seconds**
  (`budget_floored_retry_after(estimate)` on the pre-queue path = the budget it
  was refused against; measured 210–355 s in the incident)
- header **`x-anthropic-throttle-queue-timeout: 1`**
  (`config.QUEUE_TIMEOUT_HEADER`, `src/anthropic_throttle_proxy/config.py:542`)
- body (text):
  `"proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue\n"`

The body and the `retry-after` value are the *entire* signal. Note the body
literally tells the client **not** to retry: *"retrying will re-enter the fair
queue"*. The proxy's design intent is **fail fast + fair re-entry**, not
"retry me immediately".

## 3. What pi does with that 503 (receipts)

pi has **two** retry layers. Neither honors the proxy's intent for this response.

### 3.1 Provider layer — honors `Retry-After`, but off by default AND capped

`packages/ai/src/utils/provider-retry.ts`:

- `getRetryDelayMs` (line 53) reads the **HTTP header** `retry-after` /
  `retry-after-ms` (lines 54–64) — so it *would* honor our 210–355 s.
- BUT `validateServerRetryDelayMs` (line 39) **throws** when the server delay
  exceeds `maxRetryDelayMs` (default `DEFAULT_MAX_RETRY_DELAY_MS = 60_000`,
  line 1). A 210 s `retry-after` therefore becomes
  `Server requested 210s retry delay (max: 60s). …` — an error, not a wait.
- AND the layer is disabled unless configured: `retryProviderRequest` uses
  `options.maxRetries ?? 0` (line 111). pi's docs keep `retry.provider.maxRetries`
  at `0` by default (`packages/coding-agent/docs/settings.md`).

The live fleet config sets **neither** knob (§5), so this layer does not run and
does **not** even see the header.

### 3.2 Agent layer — the layer that actually runs, and it ignores `retry-after`

When the provider layer is off, the raw 503 reaches pi's **agent-level** retry:

- Classification is **string matching on the error message**:
  `packages/ai/src/utils/retry.ts`, `RETRYABLE_PROVIDER_ERROR_PATTERN`
  contains the bare token **`"503"`** (line 42), plus `"rate.?limit"` (37),
  `"500"` (40), `"retry delay"` (92), etc. Our 503 matches `"503"` → **retryable**.
  (`_isRetryableError`, `packages/coding-agent/src/core/agent-session.ts:3709`,
  delegates to `isRetryableAssistantError`.)
- The wait is `retryDelayMs(settings, attempt)` — `packages/ai/src/utils/retry.ts:128`:
  `min(baseDelayMs * 2^(attempt-1), maxAgentDelayMs ?? 60_000)`. This is pure
  exponential backoff. **It never reads the server's `retry-after`.**
- `_prepareRetry` (`agent-session.ts:3762`) loops this `maxRetries` times
  (`auto_retry_start` events), each a fresh full request.

So the server's `retry-after: 210` is discarded, and pi instead fires
2 s → 4 s → 8 s → 16 s (4 retries, §5) straight back at a queue the proxy just
said is full. **That is the burn loop.** Every one of those 4 attempts re-enters
the same fair queue and is refused again, then the turn finally fails with the
last 503.

### 3.3 The idle side

pi's HTTP idle timeout defaults to 300 s (`httpIdleTimeoutMs`, docs
`settings.md`). The proxy is designed to answer *inside* that window
(`queue_max_wait` 180 s < 300 s), so pi never idles out on a *held* request —
the failure mode here is the **retry burn**, not an idle hang. (A naive
"sleep until `retry_after` then retry" client would instead push the *next*
attempt past the 300 s patience window when `retry_after` is 355 s — see §4.)

---

## 4. Ideal client behavior when `retry_after` is present

The proxy's contract is unambiguous: **this 503 is a refusal, not a throttle.**
It refused *before* waiting precisely so the client would not burn a slot or sit
in a silent hold. Three candidate client behaviors:

| Option | Behavior | Verdict |
|---|---|---|
| **A. Sleep until `retry_after`, then single retry** | Honor `retry-after: N`, sleep N, retry once | **Reject.** `retry_after` = the budget the request was refused against (210–355 s). Sleeping that long (a) blows the 180 s `queue_max_wait` contract, (b) approaches/exceeds pi's 300 s idle patience at the top of the range, and (c) after sleeping, generations of 171–303 s mean the slot is *still likely taken* → the single retry re-enters the same wall. It converts a fast, honest refusal into a long silent stall. |
| **B. Provider-layer `Retry-After` retry (config `retry.provider.maxRetries=1`, `maxRetryDelayMs=0`)** | pi's one layer that *does* read the header | **Reject as-is.** Same re-entry problem as A; additionally `maxRetryDelayMs` and `maxRetries` are **global** — they would make pi sleep up to 355 s on *any* provider that ever sends a large `retry-after`, not just this gate. Too broad, and it still re-enters the wall. |
| **C. Bail on the marker, with a clear message (recommended)** | Treat the queue-timeout 503 as **terminal for this turn**; surface *"lane saturated — est_wait ≈ N s > queue_max_wait 180 s; the proxy refused rather than make you wait; retry when the lane drains"*; do **not** auto-retry | **Adopt.** Matches the proxy's own stated intent ("retrying will re-enter the fair queue"). The refusal is deterministic and cheap; re-entering adds no capacity. The human (or a higher-level, *health-gated, bounded* re-queue) decides when to come back — exactly the pattern the fleet already ships for the Z.AI lane (§5). |

**Recommendation: Option C.** One honest terminal failure beats four futile
re-entries. This is not "give up on transient errors" — genuine transient 429/5xx
from an *upstream* still retry normally. It is a narrow carve-out for the one
response where the proxy *tells us* the retry cannot help.

## 5. Does pi config support this today? (receipts)

**Yes — via the extension seam, and it is already built for a sibling lane.**
The fleet's parity extension (`~/NixOS/modules/home/lib/pi-parity-extension.nix`)
wires a `message_end` handler (line ~1858) that rewrites a matched 503's
`errorMessage` to a **non-retryable** terminal string. The ordering is documented
in-file (lines 1794–1815): `_handleAgentEvent` awaits the extension emit,
`_replaceMessageInPlace` mutates the message, and only then does
`_lastAssistantMessage` feed `_isRetryableError` → `_prepareRetry`. So the rewrite
*decides* the retry classification, on the shipped code path, with **zero pi
source change**.

The decision logic is pure and lives in
`~/NixOS/modules/home/lib/pi-delegate-routing.mjs`:

- `ZAI_QUEUE_TIMEOUT_MESSAGE` (line 1261) =
  `"503 proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue"`
  — **byte-identical** to our free-gate 503 body (§2), because both are emitted by
  the same `_queue_wait_timeout_response`.
- `planZaiQueueTimeoutTerminalization` (line ~1311) declines unless **all** of:
  `role === "assistant"`, `stopReason === "error"`,
  **`provider === "zai"`** (line 1317),
  **`model ∈ {glm-5.3-flash, glm-5.3}`** (lines 1319–1320),
  exact-marker match, zero produced content, zero usage.
- On match it returns `replacementErrorMessage: ZAI_QUEUE_TIMEOUT_TERMINAL_MESSAGE`
  (non-retryable) → pi stops retrying.

### 5.1 THE GAP (this slice's finding)

The terminalization is **provider/model-gated to the Z.AI lane**. The opencode
**free** models do **not** ride `zai`:

- Free models ride provider **`opencode-go`** (go gate) and **`opencode`** (zen
  gate). Receipt: `~/.pi/agent/models.json` → `providers["opencode-go"].models`
  is a 29-entry list whose entries override `baseUrl` to
  `http://127.0.0.1:8770/openai/v1` (the go gate). Free-catalog ids there include
  `beam-free`, `longcat-2.5-preview-free`, `mimo-v2.6-flash`, `glm-5.3`,
  `deepseek-v4.1-flash`, `space-bunny`, … (the go gate serves the whole free
  catalog; the zen gate is the sibling port).

Because `message.provider === "opencode-go"` ≠ `"zai"`, `planZaiQueueTimeoutTerminalization`
returns `decline("not the zai provider")`. **The free-gate 503 is never
terminalized.** It falls through to pi's agent retry, matches the bare `"503"`,
and burn-loops — exactly the 19/10 symptom. The mechanism to stop it already
ships; it is simply pointed at the wrong provider.

---

## 6. Minimal change to stop the burn-loop

Two independent, additive options. **Neither is applied by this spec** (proposal
only; a sibling/orchestrator lands the chosen one). Both are **config/extension**
edits — no proxy `src/`, no pi upstream source required for the immediate fix.

### Option A (recommended, immediate, no pi code change)

Widen the existing terminalization predicate so the **queue-timeout marker is
provider-agnostic**, or add the two opencode providers to the allowlist. Because
the marker string is emitted by the same proxy function for every gate, matching
on the marker (plus the existing zero-content/zero-usage guards) is sufficient and
safe — a *different* 503 (a genuine upstream error) will not carry this exact body.
Minimal, surgical diff in `pi-delegate-routing.mjs` (see `patch-proposal.md`):
drop the `provider`/`model` decline for this one marker, keeping the exact-text +
zero-usage guards. Add a matching `appendEntry`/notify so the human sees the
"lane saturated, est_wait ≈ N s" line.

Trade-off: this touches a NixOS-managed extension file, so it lands via the NixOS
repo (out of this repo). This spec proposes it; rollout is a separate, reviewed
step.

### Option B (durable, upstream pi patch — optional)

Make pi's **agent** retry retry-after-aware and honor an explicit "do not retry"
marker. Concretely, upstream `packages/`:

1. Thread the failed response's `retry-after` (seconds) into `_prepareRetry` /
   `retryDelayMs` so an agent retry waits `max(exponential, min(retry_after, cap))`
   instead of ignoring it — *only* when `retry_after` is small (≤ a configured
   ceiling, e.g. 30 s); above the ceiling, **do not sleep**, fail with the clear
   message (Option C). This keeps quick transient retries fast and refuses to
   stall on a huge server-requested delay.
2. Add a first-class non-retryable classification for a proxy "queue full, do not
   retry" marker (e.g. honor the `x-anthropic-throttle-queue-timeout: 1` semantics
   surfaced in `errorMessage`), so the bare-`"503"` string match stops overriding
   the server's explicit refusal.

This is the correct long-term fix but is an upstream-pi change (different repo,
different review) and is **explicitly out of scope for the no-rollout rule here**.
It is recorded so the fleet can file it upstream rather than re-derive it.

### What NOT to do

- Do **not** raise `retry.provider.maxRetries` / zero `maxRetryDelayMs` globally
  (Option B-of-§4): it re-enters the wall and broadens large-`retry-after`
  sleeps to every provider.
- Do **not** lower `retry.maxRetries` to 0 fleet-wide: that also kills legitimate
  fast retries for genuine transient upstream 429/5xx.
- Do **not** edit proxy `src/` for a client behavior (that is the other slice's
  territory).

---

## 7. Acceptance (for whichever change is landed)

- A free-gate (`opencode-go` / `opencode`) model receiving the fail-fast 503
  (exact marker + zero usage + zero content) produces **at most one** client
  action: a single terminal message, **no** `auto_retry_start` loop.
- A genuine upstream transient error (different body) on the same provider still
  retries under the existing `retry.maxRetries` budget.
- The Z.AI lane behavior is unchanged (its own terminalization still fires).
- Evidence: an extension acceptance replay (the pattern already exists —
  `planZaiQueueTimeoutTerminalization` is pure and replay-driven) asserting
  `terminalize: true` for `provider: "opencode-go"`, `model: "longcat-2.5-preview-free"`,
  and `terminalize: false` for a non-marker 503.

## 8. Non-goals

- No fleet-wide rollout, no proxy `src/` change, no pi upstream merge, no commit —
  this spec is research + proposal only.
- No change to the hold/`retry-after` server knobs (owned by the proxy slice).
- No change to `retry.maxRetries` / idle timeout defaults.
