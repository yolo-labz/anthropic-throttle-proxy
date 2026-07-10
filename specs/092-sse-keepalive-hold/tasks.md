# Tasks — Spec 092 SSE keepalive-hold

Dependency-ordered. Each task is one atomic PR (plan → eval → build+verify →
multi-model adversarial panel → squash-merge). Flip `[ ]`→`[x]` only on a
merged slice.

- [ ] T001 Keepalive-emitter primitive — add a helper that, given a `.prepared`
  aiohttp `StreamResponse`, writes SSE `: keepalive\n\n` comment frames every
  `THROTTLE_KEEPALIVE_INTERVAL_MS` (new config knob, default 10000) until
  cancelled, and a helper that writes a terminal SSE `event: error\ndata: {…}`
  frame. Pure functions/coroutines, no handler wiring yet. Add
  `THROTTLE_KEEPALIVE_HOLD` (bool, default true) + `THROTTLE_KEEPALIVE_INTERVAL_MS`
  (int, default 10000) to config.py. Unit tests: the comment frame starts with
  `:` and round-trips as an SSE comment that a reference SSE parser DROPS (proves
  the SDK-invisible property, invariant 3); the error frame parses as a single
  SSE `error` event with the expected `type`; the emitter honors the interval and
  stops cleanly on cancellation (no partial write). No behavior change to the
  request path.

- [ ] T002 Wire the keepalive-hold into the forward path (depends: T001) — for a
  STREAMING `POST /v1/messages` (parsed `stream:true`) within a non-zero
  wait-budget facing a TRANSIENT throttle (529, central queue-timeout 503, or
  concurrency 503/429), prepare the 200 `text/event-stream` client response,
  run the T001 keepalive emitter, and internally retry (respecting dispatch
  pacing + the remaining wait-budget) until an upstream 200 (then stream its body
  through the open response) or the budget exhausts (then emit the T001 terminal
  SSE error + clean EOF). Classify TRANSIENT vs BUDGET via `_budget_under_pressure`
  + `Retry-After` length; BUDGET-rejected / long `Retry-After` / non-streaming /
  budget-0 → keep today's clean error / fast-fail path unchanged. Preserve: AIMD
  does NOT shrink on a central-queue-depth hold (invariant 7) nor on 529
  (invariant 9); `MARKER_HEADER` strip in `_stream_response`; bearer never logged.
  Add `anthropic_keepalive_holds_total{outcome="streamed"|"errored"}` counter so
  the deploy-verify can confirm holds fire. Full acceptance matrix from spec.md
  (529-then-200, queue-timeout-then-capacity, 429-then-200, budget-rejected NOT
  held, non-streaming clean error, bound-exhausted SSE error, keepalive cadence,
  AIMD non-shrink). The 07/07 falsification tests (transient→no truncated write;
  bound-exhausted→SSE error not socket close) are REQUIRED and must fail without
  the fix.

- [ ] T003 Hot-tune the knobs (depends: T002) — expose `THROTTLE_KEEPALIVE_HOLD`
  + `THROTTLE_KEEPALIVE_INTERVAL_MS` in `POST /ui/config` (and `/ui/config/reset`)
  so the hold can be disabled or retuned in-process without a restart (matches
  the existing hot-tune pattern for the other limiter knobs). Surface an active
  in-flight-holds gauge in `/__throttle/health`. Test: a config POST retunes both
  knobs live; health reports active holds.
