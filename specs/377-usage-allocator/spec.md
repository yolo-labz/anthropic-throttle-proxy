# Spec 377 — Usage allocator: spread weekly usage over time (phase 1)

Status: phase 1 implemented, default OFF. Owner: p2T. Date: 31/10/2026.

## Problem
Two Claude Max accounts. Their 7d windows reset at different times. Measured 31/10 at
~50 % of the week: gmail 1.20× its elapsed pace, proton 0.94×, fleet ≈ 1.02× aggregate.
#374 (p2V) only redistributes load between accounts; it cannot reduce total burn.
Phase 1 slows the BURN rate, so the fleet stops overshooting its weekly envelope.

## Definitions
- `elapsed(t)` = fraction of the account's 7d window elapsed, from `reset_7d`.
- `allowed_7d(t)` = `elapsed(t) + burst`. Default `burst = 1/28` (≈ one 6 h slice of the week).
- Account is **ahead** when `util_7d > allowed_7d`; otherwise **on schedule**; no reading → **unknown**.
- Unknown is never treated as ahead, and never as zero.

## Requirements
R1. Pacing bucket. Ahead accounts are **spill-only**: routed to only when every other account is
    ahead too. Keys stay finite (invariant #3: the lane never fully blocks).
R2. All accounts ahead → non-priority requests get a stamped 503
    (`x-anthropic-throttle-queue-timeout: 1`) with `Retry-After` = catch-up time of the least-ahead
    account, capped by `THROTTLE_QUEUE_RETRY_AFTER_MAX_S`. Priority-lane calls still dispatch.
R3. Modes: `THROTTLE_WEEKLY_PACING=off|observe|enforce`. `off` is the default. `observe` computes
    and publishes only. Both leave routing byte-identical to `off`.
R4. Burst: `THROTTLE_WEEKLY_BURST`, finite in [0, 1]; invalid values fall back to the default.
R5. Client shares: `THROTTLE_CLIENT_SHARES=name:0.4,...`. Phase 1 **publishes only** (no enforcement).
    A client is deferred only when it is over its share AND the fleet is ahead; measured tokens only.
R6. Publication: `/__throttle/admission` gains `weekly_pacing` (read-only).

## Acceptance (from the brief)
A1. Replay: a burst of 100 requests never pushes an account past `allowed_7d + burst` while a sibling has room.
A2. All accounts ahead → non-priority 503 with Retry-After equal to the catch-up time (±1 s); priority still dispatches.
A3. `observe` changes no routing decision (byte-identical picks vs `off`).
A4. A client over its share is deferred only when the fleet is over schedule.
Mutants: `burst := inf` must fail A1 (caught: 5 tests); `Retry-After := 5` must fail A2 (caught: 2 tests).

## Scope and non-goals (phase 1)
- No enforcement of client shares (phase 2, after observe data exists).
- No change to the hot-path limiter, AIMD, or the queue itself. The defer reuses the existing stamped 503.
- Does not replace #370's balanced round-robin. The gate runs before routing, so both compose.
- Requires `THROTTLE_ACCOUNT_ROUTING` ≠ `off` (needs the routing snapshot); otherwise pacing is inert.

## Known limits (stated, not hidden)
- Routing-mode dependency: on `origin/main` the pacing gate applies to `least_loaded`/`budget_paced`
  routing. `round_robin` lands with #370; the gate composes with it once #370 merges.
- `util_7d` is a unified-window reading. Stale readings are used as-is; freshness is out of scope here.
- Burst default is a judgement, hot-tunable; the right value needs observe-mode data.
