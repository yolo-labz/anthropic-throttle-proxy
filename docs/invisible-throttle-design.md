# Making throttling invisible — SOTA-grounded design

Goal (Pedro, 05/07/2026): the client should feel like flawless "vanilla Claude"
— transient upstream errors and queue waits never surface. Honest ceiling: a
hard budget wall with *both* accounts weekly-exhausted is a supply problem no
proxy can hide; everything short of that is addressable.

## Live evidence (05/07/2026, desktop)
- 3h window: 426 OK, **21 client-disconnects**, 2 budget walls (Retry-After 16-18h).
- 25min window (under load from 4 research agents): **87 client-disconnects**,
  7×429, 5×fast-fail, 2×529, **0 central mark-down flips** (#81 holding).
- Active account at 7d=0.93 / 5h=0.74 — high, NOT walled. The failures were
  transient + queue, not budget.
- Two disconnect shapes, one root (`max_concurrent=1` storm clamp):
  - `where=post-queue elapsed_ms=0` — client gave up *while queued*, before dispatch.
  - `where=first elapsed_ms=5-18s` — client timed out *waiting for first byte* after dispatch.

## Immediate relief (DONE, hot-tune, reversible)
`max_concurrent` 1→2, `central_local_max_concurrent` 1→2 via `/ui/config`
(overrides.json, no restart). Verified: 0 disconnects / 0×429 in the 2 min after.
Rationale: concurrency changes token *rate* not *total*, so it doesn't hasten
the wall in token terms; clamp=1 guaranteed queue collapse and *wasted* budget on
abandoned half-generations. Revert: `key=max_concurrent value=1`.

## Durable fixes (this PR series), ranked by ROI × safety

### 1. SSE comment-heartbeat while queued / awaiting first byte  ← PR #82
The single highest-leverage fix. An SSE comment line (`: keepalive\n\n`) is
discarded by the Anthropic SDK's SSE parser (`_streaming.py`: `if
line.startswith(":"): return None`) and by the WHATWG spec — it resets the
client's idle timer without touching the typed event flow. OpenAI already ships
this; Anthropic users have filed for it (claude-code#45224).

Design (hot path, streaming requests only, `stream:true`):
- Decide admission/shed **before** any bytes (see #2). Once committed to serve:
  `StreamResponse.prepare()` a 200 `text/event-stream`, spawn a background task
  writing `: queued\n\n` every ~10s, acquire the fair slot, connect upstream,
  cancel the heartbeat, splice the real upstream stream through unchanged.
- **Critical ordering constraint**: `prepare()` commits a 200 status — after it
  you can no longer return a real 429/503. So all fast-fail/shed returns must
  happen before `prepare()`. A mid-stream upstream error after commit is
  delivered in-band as `event: error` (Anthropic documents this).
- Comment-only, never a typed `event: ping` before `message_start` (outside the
  documented flow). Splice upstream's own `ping` frames untouched.
- Beats the binding timers: 60-120s intermediary idle, 5-min Claude Code stream
  watchdog, 10-min SDK read timeout. 10s cadence clears all.
- Detect client-gone during the wait via `request.transport.is_closing()` and
  abandon the slot early (don't spend budget dispatching for a dead client).

Falsifier: with heartbeat active, a request held 60s in queue must NOT produce a
`client-disconnect`; the client receives `: keepalive` bytes then the real stream.

### 2. Little's-law fast-shed (clean 429 beats silent disconnect)
Before `prepare()`, estimate queue wait = `queued_ahead × avg_dispatch_interval`.
If it exceeds client patience, return `429 + Retry-After` immediately (no bytes
sent). The SDK auto-retries a 429 transparently with backoff — the request
succeeds on a later attempt instead of dying as a visible timeout. Pairs with #1:
hold+heartbeat when wait < patience, shed-clean when wait > patience.

### 3. Bounded transparent retry of 529/503/502 (pre-first-byte, jittered)
529 (overload) and transient 503/502 are Anthropic-capacity, not budget. Retry
2-4× with full jitter (`random(0, min(cap, base·2^n))`, AWS) before surfacing,
only while no bytes relayed. Cap cumulative wait to the client timeout budget.
Already have Retry-After honoring — add the bounded re-dispatch + the cumulative
cap so a 16-18h wall still fails fast while a 2s blip is absorbed.

### 4. Opus→Sonnet model-fallback tapping the separate 7d_sonnet budget
Reverse-engineered unified headers expose THREE windows: `5h`, `7d`, `7d_sonnet`.
Opus and Sonnet draw on *separate* weekly buckets. When Opus-7d is walled on an
account, an Opus→Sonnet downgrade on the SAME account taps a distinct allocation
— real added capacity, no second credential, ToS-clean. Opt-in (changes output
quality); signal via `x-throttle-degraded: sonnet` header. This is the ONLY lever
that adds effective capacity without more accounts.

### 5. Defensive unknown-status branch (latent bug)
Code branches on unified-status `allowed`/`allowed_warning`/`rejected` (our live
measurement). A community RE reports `allowed`/`exceeded`/`rate_limited`. Add a
default branch so an unknown status can't fall through the `allowed` path and
dispatch into a wall.

## Explicitly NOT doing
- Model fallback to non-Claude (changes the product; user wants "vanilla Claude").
- Response/semantic caching at the proxy (coding prompts are near-unique; rely on
  Anthropic's upstream prompt cache, which Claude Code already marks).
- Request hedging (duplicates token billing + slot burn — anti-pattern here).
- Scaling past 2 accounts (the exact behavior that triggers ban waves).
- In-flight `Authorization` rewrite is the banned-class (Architecture A) mechanism;
  the current `THROTTLE_ACCOUNT_ROUTING` sits on that line — keep pool at 2, keep
  `/login` interactive, document the residual risk. Prefer credential-at-rest swap.

## Honest ceiling
When BOTH accounts are weekly-walled: no routing, retry, or heartbeat conjures
tokens. Best possible = Opus→Sonnet (separate bucket) if it has headroom, else a
clean reset-ETA ("resets in 3h14m") instead of a raw 429. That residual is
supply, not resilience.
