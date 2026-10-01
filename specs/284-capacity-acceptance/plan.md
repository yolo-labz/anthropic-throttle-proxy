# Plan — executable seat-capacity acceptance (workspace w1P)

Base `2e7a43f`, branch `284-capacity-acceptance`. Scope: test authoring ONLY.
Owned files: `tests/test_seat_capacity_acceptance.py` + `specs/284-capacity-acceptance/`.

## Mechanism map (what already exists and is exercised)

| Matrix item | Existing mechanism | Prior unit/e2e coverage |
|---|---|---|
| static-key pool retirement A→B while B is busy | `accounts.account_snapshot`/`routing_snapshot` drop a missing credential file (PR #274 static `tp-…` pools); `proxy._route_account_if_enabled` rewrites upstream Authorization | `test_accounts.py` (snapshot/rotation), `test_proxy_helpers.py` (scoring) — no concurrent handover e2e |
| concurrent requests spanning two eligible accounts | `least_loaded` account routing + per-bearer `FairBearerLimiter` | single-request reroutes in `test_proxy_app.py`; no concurrent span test |
| exhausted/rejected account consumes no useful slots | `_unified_window_pressure` hard-gates `rejected`/`util≥1` to `math.inf` | unit-scored in `test_proxy_helpers.py`; no handler-level proof |
| differing caps and durations | per-bearer live caps (`max_concurrent`, AIMD) + slot held for the whole forward | `test_limiter.py` cap math; no mixed-cap/mixed-duration e2e |
| queued cancellation returning leases | `acquire_lease`/`_cancel_cleanup`/`_holds` lease bookkeeping | `test_limiter.py` cancel paths; no handler-level leak check |
| measured per-client fair access without exceeding caps | per-client RR (`_rr_order`, `queued_per_client`) | `test_limiter.py` RR internals; no multi-client e2e |

## Approach

One new test module driving the REAL `proxy.handler` (account routing → fair
queue → leases → dispatch) against a local recording upstream (aiohttp
`TestServer`), patterned on `tests/test_proxy_app.py` / `tests/sim/fake_anthropic.py`.
No real providers, no real credentials (dynamic `tp-<uuid>` keys in `tmp_path`).

Determinism levers (no sleeps as synchronization):

- per-tag arrival events + hold events at the fake upstream (overlap and
  ordering are proven, not sampled);
- cold-start probation cleared per seat (`try_begin_retry_probe` +
  `finish_retry_probe(success=True)`) — a tested orthogonal mechanism
  (`test_expiry_burst_makes_exactly_one_half_open_b_attempt`), kept out of scope;
- stickiness (incoming Authorization = seat token) where a tie must resolve
  deterministically; strict load differences where spanning is the claim;
- bounded waits that fail with the observed upstream order in the message.

Assertions are on actual completions (HTTP 200 + response body naming the seat),
upstream per-seat attempt/inflight accounting, and limiter books
(`inflight`, `queued_total`, `queued_per_client`, `_holds`). A test never passes
by swallowing a failure or skipping an assertion.

## Non-goals

- No production code changes (routing/meters/UI are other workers' slices).
- No model-judgment/review claims, no live inference, no browser, rbw, private
  vault or deployment. Commit only this slice; coordinator integrates in 279.
