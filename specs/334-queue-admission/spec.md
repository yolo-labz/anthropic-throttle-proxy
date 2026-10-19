# Spec 334 — Queue admission: one decision, a floor that is not a dead end

**Status:** implementation (19/10/2026), lead/queue-admission slice of the
2026-10-19 free-models mission.
**Files owned by this slice:** `src/anthropic_throttle_proxy/limiter.py`,
`src/anthropic_throttle_proxy/proxy.py`,
`tests/test_queue_admission_lead.py`. No commit.

## 1. Hypothesis (one sentence)

The go gate's lanes collapse because a 429 carries no *evidence of which limit
it was*, so every headerless pushback is read as budget pressure and
multiplicatively shrinks the concurrency ceiling into `AIMD_MIN`, where the
ramp is paid in *successes* — a currency the collapsed ceiling can no longer
earn, so the lane rests at the floor, its drain estimate predicts a wait past
the 180 s client bound, and every other arrival is refused 503.

### Receipts (live, 19/10/2026)

```
journalctl --user -u opencode-go-throttle-proxy.service --since 18:50
Oct 19 18:50:24 aimd-shrink bid=e574f38a status=429 max_concurrent=5  retry_after=0.0 pause=30.0 synthetic_pause=True (budget/real-retry-after pushback)
Oct 19 18:51:44 aimd-shrink bid=e574f38a status=429 max_concurrent=3  ...
Oct 19 18:53:15 aimd-shrink bid=e574f38a status=429 max_concurrent=2  ...
Oct 19 18:53:48 aimd-shrink bid=e574f38a status=429 max_concurrent=1  ...
... rest at 1 for 15.2 min ...
Oct 19 19:02:24 queue-wait-timeout ... reason=elapsed        max_concurrent=1 service_s=171.45 est_wait_s=342.901 retry_after_s=300
Oct 19 19:02:26 queue-wait-timeout ... reason=pre-queue-depth max_concurrent=1 service_s=173.535 est_wait_s=347.07  retry_after_s=300
Oct 19 19:02:30 queue-wait-timeout ... reason=pre-queue-depth max_concurrent=1 service_s=177.605 est_wait_s=355.21  retry_after_s=300
```

Ten `queue-wait-timeout` 503s. The decisive detail is `retry_after=0.0` on every
shrink: the upstream sent **no** Retry-After and no unified headers, and the go
gate has no plan-lane meter, so `_budget_under_pressure` had *no evidence at
all* and fell through to its conservative default — which is `True` ("assume
budget") — and `proxy.py:4034` therefore took the `else: await limiter.shrink()`
branch labelled `budget/real-retry-after pushback`. **A guess was labelled as
a measurement.**

Live shape of the same lane at 19:35:29 (`/__throttle/health`): go
`inflight=13`, bearer `e574f38a` `max_concurrent=36`, window cleared — the lane
recovers to *above* its measured 32-wide clean width whenever the floor is not
what is holding it down. That is the evidence that the ceiling mechanism is
capable, and that the floor is what fails.

### Falsifiers

- **F1** If the collapse were really about the floor being too low, raising
  `THROTTLE_AIMD_MIN` to 2 alone would have stopped the 503s. It did not: the
  same lane 503'd again at `max_concurrent=1` after the process restart
  (19:02-19:03 window above pre-dates the drop-in; the post-restart run at
  19:20-19:27 still shrank 23 → 16 → 17 → 11). A floor alone is necessary but
  not sufficient — recovery must also not be throughput-starved.
- **F2** If success-count recovery were adequate, the ceiling would have left
  the floor inside a few generations. It rested 15.2 min (watchdog replay:
  `ceiling 1 <= floor 1 for 15.2 min, dwell crossed 19:03:48`). With
  `service_s≈173` and `AIMD_RAMP_AFTER_FAST=5`, the floor earns one slot per
  ~14.5 min; that is the starvation.
- **F3** (against my own fix) If not shrinking on a *positively-evidenced*
  budget wall were safe, the ceiling would stay high during a real budget wall
  and the lane would spin. Falsified by `test_keepalive_hold.py:517`
  (`unified=rejected` 429) — `_maybe_pause_rejected` already auto-pauses the
  bearer to reset on that signal, so dispatch is gated regardless of ceiling.
  **A budget wall is therefore already gated by the window; shrinking adds only
  the collapse.**

## 2. Design

### 2.1 One admission decision — one classifier (`proxy.py`)

`_budget_under_pressure()` is currently read by *two* decisions that must agree
(`_pushback_pause` and `_aimd_apply_pushback`) and it answers a boolean, which
cannot tell "measured budget wall" from "no evidence". Replace it with a
single evidence classifier used by pause, AIMD **and** the retry gate:

```python
PushbackEvidence = Literal["retry-after", "budget", "concurrency", "unknown"]

def _pushback_evidence(meta, bid) -> PushbackEvidence
```

| class | positive evidence | AIMD | pause |
|---|---|---|---|
| `retry-after` | upstream sent `Retry-After` | shrink (width signal) | window, uncapped |
| `budget` | unified `allowed_warning`/`rejected`/binding util ≥ warn, **or** a fresh *measured* plan meter at/over its line | **no shrink** — the window is the gate | `AIMD_BACKOFF_S` |
| `concurrency` | unified `allowed` below warn, or fresh metered headroom | shrink (floored) | `CONCURRENCY_COOLDOWN_S` |
| `unknown` | none of the above | shrink (floored) | `AIMD_BACKOFF_S` |

`unknown` keeps the historical *shrink* (opencode's 429s are its rate limit and
must still shed width) but is now **logged as unknown** rather than as
`budget/real-retry-after pushback`, so the next incident does not have to
re-derive that the label was a guess. The one behaviour change with teeth is
`budget`: a measured budget wall no longer costs concurrency.

### 2.2 The floor is a resting place (`limiter.py`)

- `FairBearerLimiter.aimd_floor()` = `min(self.hard_max, config.AIMD_MIN)` is
  the single source for the floor (the operator's hard ceiling still wins —
  sibling's fix retained, now named).
- **Floor-dwell ramp.** `_may_grow()` gains one clause: at the floor, after the
  cooldown and with **one** clean success, a single slot is earned. The
  rationale is the positive feedback loop in §1: the ceiling sets throughput and
  throughput is the ramp's currency, so lowering the ceiling lowers the recovery
  rate. Above the floor nothing changes — the success-count ramp (FAST/SLOW,
  `tests/test_aimd_free_lane_policy.py`) still owns recovery, and a floor pinned
  by a `hard_max` floor still cannot grow.

### 2.3 No raw 429 to the client (`proxy.py`)

After the pushback retry budget is spent, the upstream 429 is currently relayed.
A client that does not honour `Retry-After` (pi: 4 × 15 s) turns that into a
retry storm — the second half of the incident. Change the terminal answer:

- wait budget left → latch the window and re-enter the admission path (the
  request parks invisibly); bounded by `wait_deadline` and by
  `PUSHBACK_ABSORB_MAX` so it can never loop.
- budget spent → the **existing bounded 503** from
  `_queue_wait_timeout_response`, whose `x-anthropic-throttle-queue-timeout: 1`
  stamp spec 333 and the `:8760` ingress both depend on (contract preserved).

Deliberately still relayed, unchanged: the OAuth **entitlement refusal** (a
request-shape gate, no amount of waiting changes it) and **OAuth telemetry**
429s (a different endpoint's limit, documented 13/07 incident). Neither is a
quota violation, and both have consumers that need the real status.

## 3. Acceptance

1. **(a)** Under repeated pushback, the ceiling never rests below
   `aimd_floor()`, and a lane at the floor regains a slot within one cooldown +
   one clean success.
2. **(b)** With a fake upstream that 429s above width N, sustained parallel load
   converges to a ceiling that stops producing 429s, and **no 429 reaches the
   client** — refusals, when any, are the stamped 503.
3. **(c)** A `Retry-After` window ≤ `MAX_HOLD_RETRY_AFTER_S` (240 s live) still
   holds the request and returns a queued success.
4. Full `pytest` + `ruff check`/`format` clean, and the sibling slices'
   suites (`test_aimd_free_lane_policy.py`, `tests/test_free_gate_watchdog.py`)
   stay green.

## 4. Out of scope

- NixOS-side hold=240 (branch 2902) — treated as the contract, not re-derived.
- Cross-gate spillover (spec 333) — decided deferred, see
  `specs/333-free-gate-spillover/decision-2026-10-19.md`.
- The watchdog's systemd timer + floor-metric redeploy (fgw slice's hand-off),
  recorded in `specs/334-queue-admission/integration-notes.md`.
