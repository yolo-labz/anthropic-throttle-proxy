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

The arriving request needed two service rounds behind three queued peers. With
a service time of ~113 s the earliest possible start was ~227 s — 7.6× the
30 s budget. It was hopeless before it was parked, and the lane already held
every number needed to know that. This was saturation, not a dead proxy and
not an upstream quota wall.

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

## 4. Post-change results

Filled in by `verify.sh` and recorded at merge time.

- RED→GREEN test: `tests/test_queue_depth_admission.py`
- Full suite, ruff check/format: see PR body.
- Live verification is **not** claimed by this slice: the desktop `:8766`
  service runs a Nix-pinned build. It stays on the old behavior until
  `~/NixOS` pins the merged rev and the host activates it — a separate,
  Pedro-gated slice. The row's `done_oracle` (positive
  `saturation.queue_admit_max_depth` from live `:8766`) can only pass after
  that.
