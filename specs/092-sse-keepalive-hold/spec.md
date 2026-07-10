# Spec 092 — SSE keepalive-hold: make transient throttle errors invisible

**Status:** slice 2 of the "invisible throttle" north-star (slice 1 = PR #91,
usage-poller self-429 backoff, merged `dffd56e`).

## Problem (live-diagnosed 10/07/2026, `/metrics` + journal)

Claude Code shows two retry banners whenever the proxy hands the SDK a
transient throttle error:

- `✻ API error · Retrying in 0s · attempt 1/10` — a central **queue-timeout
  503** relayed to the client under fleet saturation, and 503/429
  **concurrency-cap** pushback exhaustion passed through.
- `Server is temporarily limiting requests (not your usage limit) · Rate
  limited` — a **529 upstream-overloaded** (Anthropic's own capacity, *not* the
  account budget) passed through after one internal retry.

Each hard status the proxy returns/relays for a streaming request = one banner.
Sample this run: `POST opus-4-8 503`×10, `sonnet 503`×2, `fable 503`×1, + 429s.

These are **transient** (concurrency, central queue depth, Anthropic overload) —
capacity returns in seconds. Passing them through is the visible cost of the
07/07 decision to return a clean early error rather than hold silently.

## Goal

For a **streaming** `POST /v1/messages`, when the proxy would otherwise return
an **unprepared** transient throttle error (529, central queue-timeout 503, or
concurrency 503/429), instead **commit a `200 text/event-stream` response and
emit SSE `:` comment keepalives** while internally retrying/waiting for
capacity, so the SDK never sees an error. On the eventual upstream 200, stream
the real body through the already-open response. Bound the hold **end-to-end**
(reuse the existing wait-budget). Fall back to a clean error **only** for
genuine budget-rejected (unified `rejected` / long `Retry-After`) or when the
bound exhausts — and then emit a well-formed SSE `event: error` + clean EOF,
never a bare mid-message socket close.

Net effect: a slow (held) response instead of a banner — the north-star trade.

## Hard constraints / invariants (each maps to an acceptance test)

1. **07/07 footgun.** A *silent* hold (no bytes to the client) past ~60 s makes
   claude abort → the proxy's late write hits a closing transport → truncated
   HTTP → `InvalidHTTPResponse` → phantom 401/login storm. Keepalive `:`
   comments MUST flow at an interval well under the client idle timeout
   (default ≤ ~10 s) so the idle timer never trips. Keepalive-with-heartbeat is
   strictly safer than the old silent parking.
   **Falsification:** after the fix, a simulated transient 529/queue-timeout
   that clears within the bound MUST yield a clean 200 SSE stream with **no
   banner and no truncated write**; a hold that exhausts the bound MUST end with
   a well-formed SSE error event, **never a bare socket close mid-message**.
2. **Hold only for TRANSIENT pressure.** Classify via the existing
   `_budget_under_pressure` (unified `anthropic-ratelimit-unified-*` headers) +
   `Retry-After` length: budget-rejected / long `Retry-After` → do NOT commit to
   keepalive (keep today's clean error / fast-fail — regression-guarded).
   Concurrency-cap / central-queue-depth / 529 → hold.
3. **SSE `:` comments are dropped by the Anthropic SDK parser** (the verified
   north-star mechanism). Emit `: keepalive\n\n`. Do NOT fabricate
   message/content events.
4. **Irreversible commit.** Once the 200 SSE response is `.prepared`, an HTTP
   error status can no longer be sent. The bound-exhausted tail MUST degrade to
   an SSE `event: error\ndata: {…}` then clean EOF.
5. **Non-streaming requests** (no `stream:true` in the parsed body) CANNOT get
   SSE keepalive → keep current clean-error behavior (regression-guarded).
6. **End-to-end wait-budget preserved.** `x-anthropic-throttle-wait-budget-ms` /
   `_effective_queue_max_wait` / the `WAIT_BUDGET_HEADER` `min()` logic (PR #83
   Codex BLOCKER) — the hold consumes the SAME budget; it must NOT stack a fresh
   window past client patience. budget 0 → no hold.
7. **Central-relay semantics.** `_is_queue_timeout_response` marks a central
   queue-timeout 503 relayed verbatim + exempt from `_should_retry_pushback` /
   AIMD today. The hold changes *client-facing* behavior (keepalive vs relay)
   but must NOT AIMD-shrink this bearer on a central-queue-depth signal (that is
   admission backpressure, not this bearer's upstream pushback) and must keep
   the anti-spoof `MARKER_HEADER` stripping in `_stream_response`.
8. **Health invariant #4** (`/__throttle/health` < 50 ms) untouched — this is
   request-path only. Bearer token never logged. `CollectorRegistry` stays
   process-local.
9. **529 does NOT AIMD-shrink** (`anthropic_overload_total`) — preserve while
   adding the hold.

## Key code (`src/anthropic_throttle_proxy/proxy.py`)

- `handler` (~1619) streams what `_forward_with_retry` (~1155) returns.
- `_should_retry_pushback` (~1134) decides retry-vs-relay;
  `_is_queue_timeout_response` (~714) detects the central relay-503.
- `_try_forward` / `_forward_once` build the `web.StreamResponse`;
  `response.prepared` == committed-to-client. Throttle errors return an
  **unprepared** `web.Response` — the clean insertion point.
- `_retry_after_fast_fail_response` (~1373), `_maybe_fast_fail_throttle_direct`
  (~1040) — the fast-fail 429/401 paths.
- `_stream_response` strips the marker header.
- `config.py`: `THROTTLE_STATUSES`, `OVERLOAD_STATUSES={529}`,
  `RATE_PUSHBACK_RETRIES`, `WAIT_BUDGET_HEADER`.
- New knobs: `THROTTLE_KEEPALIVE_HOLD` (bool, default `true`),
  `THROTTLE_KEEPALIVE_INTERVAL_MS` (default `10000`); max-hold reuses the
  wait-budget. Hot-tunable via `/ui/config` where practical.

## Acceptance test matrix (`tests/`, aiohttp test client + fake upstream)

- **529-then-200**: client gets 200 SSE with keepalive comment(s) then real
  events; no error status.
- **central queue-timeout 503-then-capacity**: held + streamed; no relay-503 to
  the client.
- **concurrency 429-then-200**: held + streamed.
- **budget-rejected** (unified `rejected` / long `Retry-After`): NOT held →
  clean error as today (regression guard).
- **non-streaming under throttle**: clean error, no keepalive.
- **bound exhausted mid-hold**: ends with SSE `event: error`, well-formed, no
  truncated write.
- **keepalive cadence**: comment emitted at < idle-timeout interval.
- **AIMD**: a central-queue-depth hold does NOT shrink the bearer; a real
  upstream 429 still does.

## Done-state

All acceptance tests pass; `ruff check` + `ruff format --check` clean; the
multi-model adversarial panel ALLOWs (Codex must specifically challenge the
07/07 truncation path, the irreversible 200-commit point, transient-vs-budget
classification, the SSE error tail, wait-budget non-stacking, and AIMD
non-shrink on central-queue-depth). Deploy = Nix pin bump (pane 19) — verify a
simulated transient throttle yields no banner on `:8765`.
