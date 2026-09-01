# Evidence: FLEET-17 queue-depth admission

All numbers below are lane counters and timings. No session text, prompt
content, credential, or client payload appears here or anywhere in this slice.

## 1. Measured incident (01/09/2026 14:20–14:24 BRT, live `:8766` Z.AI lane)

| Fact | Value |
|---|---|
| Live cap | `max_concurrent = 2` |
| Queue depth at arrival | 3 |
| Inherited effective wait budget | 30 s |
| Completion of the two occupied slots | 113.7 s and 221.2 s |
| Proxy answer | `503` `queue-wait-timeout`, `Retry-After: 5` |
| Client behavior | retried the same lane 4×, then aborted |
| Persisted service knob | `THROTTLE_QUEUE_MAX_WAIT_S=180` |
| Health after the window | queue 0 / inflight 0 |

Three requests were already parked and neither occupied slot freed for 113.7 s
and 221.2 s after it started, against a 30 s budget. The record does not say where the arrival sat in the per-client rotation, nor how old the two holders were at that instant, so the exact wait it faced is not recoverable; what is recorded is that neither slot freed for 113.7 s and 221.2 s after it started, and that the proxy answered only after burning the whole budget. The
lane's own live numbers — slot count, occupancy, per-client queue, and how long
its slots had already been held — were sufficient to estimate that wait before
parking anything, and none of them were consulted. This was saturation, not a
dead proxy and not an upstream quota wall.

## 2. Falsifier replay on current `main` (01/09/2026, offline, no service touched)

Brief's falsifier: *if a deterministic replay with 2 occupied slots, 30 s
remaining budget, and measured service time > 60 s already rejects before queue
with a truthful bounded retry interval on current main, no producer change is
needed.*

Replay at 1/100 time scale (`max_concurrent=2`, 3 queued, budget 0.30 s,
holders 1.137 s / 2.212 s), driving the real `FairBearerLimiter` and the real
`proxy._queue_wait_timeout_response`:

```
[anthropic-throttle] queue-wait-timeout bid=bid00000 cid=arriving path=/v1/messages max_wait_s=0.3 inflight=2 queued_total=3 max_concurrent=2
pre-arrival: inflight=2 max_concurrent=2 queued_total=3
RESULT: 503 after 0.301s of a 0.300s budget (scaled: 30.1s of 30s)
        parked-not-rejected=True
        retry-after=5  (x-anthropic-throttle-queue-timeout=1)
        real drain for this arrival >= 4.42s scaled (442s real) — 2 rounds behind 3 queued
```

**Verdict: not falsified.** Current `main` (a) admits the impossible request
and burns the entire budget in silence, and (b) advertises a constant 5 s
retry against a ≥ 442 s real drain. The producer change is required.

## 3. What the fix must therefore establish

1. The same replay rejects **before** enqueue — `queued_total` unchanged, no
   slot consumed, no counter leak.
2. The `503` advertises an interval that is at least the exhausted budget, so a
   compliant client cannot re-enter the same wall immediately.
3. The bound and its inputs are readable from `/__throttle/admission` so a
   consumer can see saturation without re-deriving it (the repeated failure
   mode this repo already documents for availability).

## 4. Codex adversarial review, round 1 (BLOCK at `36015f9`)

Different-family gate (this is Anthropic-authored work). Seven findings; every
one acted on. Two are worth recording because they changed the design, and one
because it was refuted with evidence rather than accepted.

| Finding | Disposition |
|---|---|
| BLOCKER — admission not atomic with enqueue; a burst could all pass one stale estimate | **Refuted with a probe.** Every `_lock` critical section in the limiter is synchronous, so `asyncio.Lock.acquire` takes its non-yielding fast path and check+enqueue complete in one event-loop step. Measured: 10 simultaneous arrivals against a 1-slot evidenced lane → 2 enqueued, 8 refused, exactly the published bound. Pinned by `test_simultaneous_burst_is_serialized_not_all_admitted` so a future `await` inside a critical section breaks the test rather than the guard. |
| BLOCKER — depth math assumed FIFO, but the dispatcher is per-client round-robin | **Accepted, real regression.** A new client behind one chatty client's 20 parked requests would have been refused although RR serves it on the very next release. Fixed with `_ahead_of(client_id)`: own depth + `min(depth_j, own + 1)` per sibling. Both directions pinned by `test_round_robin_position_not_raw_queue_depth`. |
| BLOCKER — FIFO stamp pairing could pop the long holder's stamp on a short completion, erasing the incident signal | **Accepted.** Replaced with per-slot leases (`_holds: lease -> (lane, start)`), threaded through `acquire_lease`/`release`/`_cancel_cleanup`. Bookkeeping is now bounded by real in-flight count instead of a 256-entry truncation. |
| BLOCKER — a heuristic was named and documented as proof | **Accepted.** Language corrected across spec, docstrings and log line; added `residual_s` so a wave that is nearly finished is not charged a whole round, and stated the accepted false-refusal cost explicitly. |
| MAJOR — evidence did not establish the *first* arrival would have been refused | **Accepted, claim narrowed.** See §5. |
| MAJOR — published bound uses the configured 180 s horizon while the hot path used the effective 30 s | **Accepted**, documented as configured-horizon advisory capacity with `max_wait_s` naming the horizon. |
| MAJOR — the Retry-After "ceiling" was not a ceiling; the elapsed path charged already-spent budget again | **Accepted.** Hard cap in both paths; the budget floor now applies only to the pre-queue refusal (`budget_floored_retry_after`). |
| MINOR — non-finite / zero inputs | **Accepted.** `_finite()` guards, `_MIN_SERVICE_TIME_S` floor, regression tests for `1e400`/`nan`/negative budgets and a zero service estimate. |

## 5. Scope of the causal claim (narrowed twice, after both review rounds)

This change would **not** necessarily have refused the very first arrival of
the incident, and the incident record is not sufficient to decide it. Two
inputs the estimator needs were never captured: the per-client round-robin rank
of the arrival (only the total depth of 3 was recorded) and the holders' AGES
at that instant (only their eventual 113.7 s / 221.2 s totals). With fewer than
three completions and young holders the estimator is cold and does not enforce
at all.

The two arithmetic tests are therefore **synthetic bounds under stated
assumptions**, not a replay of the event:
`test_incident_arithmetic_at_the_measured_scale` and
`test_rejects_at_the_lanes_own_measured_service_rate` both assume `ahead = 3`
(true when the three queued requests belong to distinct clients, or to the same
client as the arrival) and fresh holders (`residual = service`).

What the change does establish:

- At the lane's own measured rate from the ledger's 29/08 observation
  (~35 s/req on 2 slots) with those assumptions, the shape refuses at once:
  70 s of scheduled wait against a 30 s budget.
- By the second of the four retries the holders were already ~100 s old —
  evidence on its own — so a repeat of the storm is cut at the first retry.
- The client's outcome changes from *four silent 30 s stalls then an abort* to
  an immediate 503 carrying an honest interval.

## 5a. Codex adversarial review, round 2 (BLOCK at `c16cf21`)

| Finding | Disposition |
|---|---|
| BLOCKER — a scalar residual misprices staggered holders in BOTH directions (waves an arrival through on a nearly-done slot when the stuck one is what it waits for; refuses one that the other slot serves in a second) | **Accepted.** Residuals are now per holder, and the wait is a scheduled estimate: each server is "free at T", requests take the earliest slot (`_wait_for_position` / `_admissible_ahead`, min-heap, bounded at 1024 simulated dispatches). Both directions pinned by `test_staggered_holders_are_priced_per_slot`. |
| MAJOR — RR position ignored `_rr_order`, so a client already at the head was over-counted | **Accepted.** Siblings ahead of the arrival's rotation position contribute up to `own + 1`, siblings behind contribute up to `own`. Pinned by `test_round_robin_counts_the_rotation_not_just_the_clients`. |
| MAJOR — an `off`/`observe` bearer published fabricated enforced capacity | **Accepted.** Bypass bearers are counted and force `enforced: false` with `source: bypass`; the number stays positive but is explicitly advisory. |
| MAJOR — the narrowed incident claim was still unsupported | **Accepted**, narrowed again — see §5 above. |
| MINOR — an unknown non-null lease popped another LIVE lease | **Accepted.** The oldest-hold fallback is now reachable only for a leaseless caller; an unknown lease logs and takes nothing. |
| MINOR — a configured `1e400` still reached the published JSON as `Infinity` | **Accepted.** `config._finite_env_float` validates at parse time, which also fixes the pre-existing `saturation.queue_max_wait_s` field. |
| MINOR — docs contradicted the repaired behavior | **Accepted.** README, CLAUDE.md and the spec now describe the lease model, the schedule, and the two Retry-After floors; "provably/by construction" removed. |
| Verified positively by round 2 | The round-1 atomicity refutation stands (no reachable yield between estimate and enqueue); lease migration across a priority-reserve `1→0` hot-tune finishes with zero holds and correct per-lane attribution; retry hard-capping and the elapsed/pre-queue floor split are correct. |

## 5b. Codex adversarial review, round 3 (BLOCK at `1878590`)

| Finding | Disposition |
|---|---|
| BLOCKER — the schedule treated every busy holder as a server, but `busy > slots` is normal after an AIMD shrink, and `_try_dispatch` stays shut until occupancy falls back UNDER the cap. With `slots=1`, `busy=2`, residuals `[1, 100]` the model admitted on a completion the dispatcher ignores | **Accepted.** The earliest `busy - slots` completions are dropped before scheduling — they only pay down the overshoot. Pinned twice: `test_oversubscribed_pool_waits_for_the_cap_not_the_first_completion` (arithmetic) and `test_shrunk_limiter_does_not_dispatch_on_the_first_release` (real limiter, shrink 2→1). |
| MAJOR — the round-2 narrowing was applied only to `evidence.md` §5; spec/plan/CLAUDE.md/config/test docstring and the PR body still said the arrival needed two rounds or was impossible "by construction" | **Accepted.** Every one of those statements now carries the same caveat: the record fixes neither the rotation rank nor the holders' ages, so the exact wait is not recoverable. What is claimed is only what was recorded. PR body rewritten to the RR-schedule + lease design. |
| MINOR — hot-tuning still accepted `nan` (it compares false against both bounds) | **Accepted.** `config._coerce` rejects non-finite floats before the bounds check; `test_hot_tuned_knob_rejects_a_non_finite_float`. |
| MINOR — a mixed fair+bypass lane did not set `source: bypass` | **Accepted.** Any bypass bearer sets `source: bypass` and keeps the queueing bearers' provenance as `fair_source`; `test_mixed_fair_and_bypass_lane_reports_bypass`. |
| NIT — stale `_service_estimate` return annotation | Fixed. |
| Verified positively by round 3 | RR position matches `_try_dispatch` including tail re-append, emptied deques, newcomers and `_priority_rr`; unknown leases no longer consume another hold; residual clamping, zero-slot normalization order and the `busy <= slots` heap are correct; the 1024-step bound costs ~1.14 ms for a synthetic worst-case bearer. |

## 6. Post-change results

Filled in by `verify.sh` and recorded at merge time.

- RED→GREEN test: `tests/test_queue_depth_admission.py`
- Full suite, ruff check/format: see PR body.
- Live verification is **not** claimed by this slice: the desktop `:8766`
  service runs a Nix-pinned build. It stays on the old behavior until
  `~/NixOS` pins the merged rev and the host activates it — a separate,
  Pedro-gated slice. The row's `done_oracle` (positive
  `saturation.queue_admit_max_depth` from live `:8766`) can only pass after
  that.
