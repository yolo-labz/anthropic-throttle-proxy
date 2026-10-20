# AIMD policy for the OpenCode FREE lanes — slice A (limiter AIMD knobs)

Owner: AIMD-policy slice of the 19/10/2026 free-model width effort.
Scope: `FairBearerLimiter` AIMD parameters (`limiter.py`) and the policy they
implement. **Not** in this slice: `proxy.py` pre-queue-depth admission
(p1H), Nix gate configuration (host change), lane economics.

Ground truth: live incident 19/10 ~18:57-19:06 on the go gate `:8770` —
budget-pushback 429 → `aimd-shrink` pinned the shared bearer `e574f38a` to
`max_concurrent=1` → with 171-303 s generations every other arrival got a
pre-queue-depth 503 (`est_wait` 210-355 s > `max_wait` 180 s) → pi panes
died in retry loops.

## 1. The one-line hypothesis

**On a free lane whose generation time exceeds the client's wait budget, the
AIMD floor is not a safety net — it is the effective admission width**, and
every arrival beyond that width is answered with a 503 instead of queued.

Everything below follows from that. It is stated once, tested in
`tests/test_aimd_free_lane_policy.py`, and falsified against the pre-fix code
in §5.

## 2. Why the floor becomes the admission width

The pre-queue-depth guard asks `drain_estimate`: can one more arrival reach a
slot within `max_wait`? With the lane saturated at its floor and every held
slot priced at residual `R`, the arrival at round-robin rank `ahead` starts at
the `ahead + 1`-th slot-free event, i.e. at `ceil((ahead + 1) / floor) * R`.

Measured with the real `_compute_drain` (generation 200 s, `max_wait` 180 s):

| slot age | floor | ahead=0 | ahead=1 | ahead=2 | ahead=4 | ahead=8 |
|---|---|---|---|---|---|---|
| 0 s (fresh) | 1 | 503 | 503 | 503 | 503 | 503 |
| 0 s (fresh) | 2 | 503 | 503 | 503 | 503 | 503 |
| 0 s (fresh) | 12 | 503 | 503 | 503 | 503 | 503 |
| 50 s (R=150) | 1 | OK | 503 | 503 | 503 | 503 |
| 50 s (R=150) | 2 | OK | OK | 503 | 503 | 503 |
| 50 s (R=150) | 6 | OK | OK | OK | OK | 503 |
| 50 s (R=150) | 12 | OK | OK | OK | OK | OK |
| 180 s (R=20) | 2 | OK | OK | 503 | 503 | 503 |
| 180 s (R=20) | 12 | OK | OK | OK | OK | OK |

Two conclusions, both load-bearing:

1. **When `R > max_wait` the queue cannot absorb depth at all.** Not one
   arrival gets in, at any floor. A 200 s generation arriving into a just-started
   lane is a 503 no matter how wide the semaphore is.
2. **While `R ≤ max_wait`, the floor is the queue depth.** The lane admits
   exactly `floor` arrivals per generation (plus whatever depth fits in
   `max_wait / R` rounds). Floor 1 ⇒ one pane is served and every sibling
   503s — that is the 19/10 incident, restated as arithmetic.

So for free models (measured 171-303 s generations) against pi's 180 s queue
budget, **the floor must be sized to the number of long-generation panes you
are willing to run concurrently**, because the queue will not carry the rest.

## 3. Live receipts (19/10/2026 19:30 BRT)

Gate env, read from the **effective merged** env
(`systemctl --user show <unit> -p Environment`; base unit and drop-ins are
merged, so this is what the process actually sees — not what either file
says alone):

| gate | port | `CLAUDE_API_THROTTLE_MAX` | `AIMD_INITIAL` | `AIMD_MIN` | `QUEUE_MAX_WAIT` | `HOLD_RETRY_AFTER` | source |
|---|---|---|---|---|---|---|---|
| anthropic (paid) | :8765 | 5 | 6 (→clamp 5) | 1 | 5 | 240 | `throttler-gateway.nix` + 8 drop-ins |
| z.ai (paid) | :8766 | 12 | 12 | 1 (default) | 180 | default | `zai-throttle-proxy.nix` |
| mimo (paid plan) | :8773 | 20 | 20 | **2** | 180 | 240 | `mimo-throttle-proxy.nix` |
| opencode **go** | :8770 | 12 → **1024** (drop-in `99-max-concurrent.conf`) | 12 | **2** (drop-in `98-aimd-floor2.conf`, 19:10 today) | 180 | 240 (drop-in) | `opencode-throttle-proxy.nix` + drop-ins |
| opencode **zen** | :8774 | 12 → **1024** (drop-in) | 12 | **1 (default — no floor drop-in)** | 180 | 240 (drop-in) | same |

Reading `systemctl --user cat` alone would have mis-stated three of these:
the anthropic unit lists both `THROTTLE_QUEUE_MAX_WAIT_S=5` and `=90`, and
both `MAX_HOLD=60` and `=240`; the merged winner is the drop-in value in each
case (5 / 240). mimo listens on **:8773**, not :8772.

Live limiter state at capture:

```
:8770 bearers.e574f38a.limiter =
  19:19 → {max_concurrent: 20, recent_shrinks: 1, successes_since_throttle: 0}
  19:30 → {max_concurrent: 24, hard_max: 1024, successes_since_throttle: 2,
           recent_shrinks: 0, storm_mode: false, effective_ramp_after: 10}
:8774 bearers.e574f38a.limiter =
  {max_concurrent: 8, hard_max: 1024, successes_since_throttle: 0,
   recent_shrinks: 0, storm_mode: false, effective_ramp_after: 10}
```

On `:8770` between those two samples: `served` 115 → 526 (411 requests, no
further 429) and the cap climbed 20 → 24, i.e. `+1 per RAMP_AFTER` clean
successes walked it up while traffic kept flowing. The unit was restarted at
19:10 with `AIMD_INITIAL_CONCURRENT=12`, so the 12 → 20 leg happened under
the same ladder.

Two facts follow immediately and are **not** opinions:

* **The zen gate is still at floor 1.** It has not yet taken the pushback the
  go gate took at 18:57, so it is armed with the exact configuration that
  failed on go one hour ago.
* **The effective hard ceiling on both free gates is 1024, not the Nix-declared
  12.** The drop-in `99-max-concurrent.conf` (11:29 today) overrides it. With
  warm start 12 and `+1 per 10 clean successes`, AIMD walks upward until the
  upstream says no.

## 4. Recommended default table

`free` = opencode go/zen lanes serving the `*-free` catalog. `paid` =
Anthropic / z.ai / mimo plan gates, kept for contrast (their economics differ:
a paid 429 is usually budget, not a per-tenant concurrency line).

| knob | free lane | paid lane (live) | rationale for the free value |
|---|---|---|---|
| `THROTTLE_AIMD_MIN` (floor) | **≥ expected simultaneous long-generation panes** (2 live on go; **recommend 4-6** for a multi-pane fleet, and 2 minimum) | 1 (anthropic/z.ai), 2 (mimo) | §2: with generation > wait budget the floor IS the admitted depth. Floor 1 guarantees the 19/10 collapse shape. |
| `THROTTLE_AIMD_INITIAL_CONCURRENT` | 12 | 5-20 | Warm start at the measured-clean width (17/10: 2/4/8/12/16/24/32 all returned 200). Do not warm start below the floor's intent — it just defers the same collapse. |
| hard ceiling `CLAUDE_API_THROTTLE_MAX` | **the policy line = measured-clean width (32)**, not 1024 | 5 / 12 / 20 | AIMD is *reactive*: it can only learn the upstream line by crossing it. A 1024 ceiling means the first crossing pays the real 429. If the mission is "queue, never cross the line", the line must be the hard ceiling. |
| `THROTTLE_AIMD_DECREASE` | 0.7 | 0.6 (anthropic) | 12 → 8 → 5 → 3 → floor: four shrinks to reach the floor, so a short 429 burst costs width in ~2 min of backoff windows, not instantly. |
| `THROTTLE_AIMD_BACKOFF_S` | 30 | 30 | Also the lookback window multiplier for storm detection (`2 × BACKOFF`) and the recovery gate. Changing it moves three behaviours at once. |
| `THROTTLE_AIMD_RAMP_AFTER` (SLOW) | 10 | 6 (anthropic) | The **only** ramp a free lane actually uses — see §6. |
| `THROTTLE_AIMD_RAMP_AFTER_FAST` | 5 | 5 (default) | Intended for an isolated 429, but unreachable on free lanes (§6). Keep the default; do not expect it to help. |
| `THROTTLE_AIMD_STORM_THRESHOLD` | 3 | 3 (default) | ≥3 shrinks inside `2 × BACKOFF` ⇒ SLOW ramp. On a lane where generations are longer than that lookback, storm and isolated look identical to the ramp logic. |
| `THROTTLE_QUEUE_MAX_WAIT_S` | 180 (unchanged, client contract) | 90 (anthropic) | Not an AIMD knob, but it is the other half of §1: a wait budget below the generation time turns the semaphore into the admission gate. |

### The hard ceiling is the policy line

This is the one recommendation to take seriously. `AIMD_INITIAL_CONCURRENT`
and the floor live *below* the ceiling; nothing stops AIMD from walking to
1024 except a 429 from upstream. If the required behaviour is "maximum
parallel width **without ever triggering** opencode's rate limit", then the
safe measured width (32, 17/10) is the hard ceiling, and the floor + warm start
decide how the lane behaves *below* that line after a pushback. Leaving the
ceiling at 1024 converts the policy into "probe the limit with real traffic".

## 5. Floor/ceiling coherence (code change in this slice)

`FairBearerLimiter.shrink()` used `max(config.AIMD_MIN, ...)`, so a floor
*above* the operator's hard ceiling **raised** the live cap during a 429. The
cap had already been clamped down by `_retune_limiter_hard_max` (a
`/ui/config` `CLAUDE_API_THROTTLE_MAX` drop below the floor), and the next
pushback inflated it back above the ceiling — with `grow()` unable to correct
it, because grow only ramps while `max_concurrent < hard_max`. A 429 was
becoming a capacity grant.

Shipped as the named helper `FairBearerLimiter.aimd_floor()` returning
`min(self.hard_max, config.AIMD_MIN)`, consumed by `shrink()` (and, since,
by the floor-dwell ramp added in spec 334) — a floor computed once rather
than per-call-site. Unchanged whenever `AIMD_MIN <= hard_max`, i.e. for every
gate in §3 today.

Falsified against the pre-fix body (`hard_max` hot-tuned to 1, floor 2):

```
PRE-FIX: hard_max=1 caps=[2, 2, 2] final=2 -> VIOLATION cap>hard_max
FIXED:   hard_max=1 caps=[1, 1, 1] final=1 -> ok
```

Fix: `aimd_floor()` = `min(self.hard_max, config.AIMD_MIN)`. Unchanged
whenever `AIMD_MIN <= hard_max`, i.e. for every gate in §3 today.

## 6. Recovery: which ramp a free lane actually gets

`_recent_shrinks()` looks back `2 × AIMD_BACKOFF_S` = 60 s. A free-lane
generation is 171-303 s, so the first clean 2xx lands *after* the shrink has
aged out: `recent_shrinks == 0` ⇒ `_effective_ramp_after() == AIMD_RAMP_AFTER`
(SLOW). **The FAST ramp is reachable only through completions of requests that
were already in flight when the shrink happened** — it is not the recovery
mechanism a free lane should be credited with. Pinned by
`test_free_lane_generation_outlives_the_fast_ramp_window`.

Time to walk from the floor back to the warm start:

```
time ≈ AIMD_RAMP_AFTER × (initial − floor) × generation_time / floor
```

At floor 2, warm start 12, 200 s generations: `10 × 10 × 200 / 2 = 10 000 s`
≈ **2.8 h**. Against a short-request mix the same ladder is minutes — the
live check above: 411 requests in 11 min bought +4 slots. So the floor is not
just the width during a stall, it is the width for **hours** after one, unless
the traffic mix is short.

That is the argument for sizing the floor to the steady-state width you want
rather than to the smallest value that keeps the lane breathing (mimo's 2,
26/09 rationale) — a free lane serving 200 s generations does not climb back
quickly enough for 2 to be a temporary state.

## 7. Tests

`tests/test_aimd_free_lane_policy.py` (12 tests, free-lane knobs):

* warm start is the initial cap, not the ceiling;
* floor pins under sustained pushback (never rests at 1);
* one budget 429 costs one step, not a collapse;
* shrink still cuts ≥1 slot above the floor;
* observe mode respects the floor too;
* **the 429 ceiling regression** (§5) + grow cannot exceed the ceiling;
* probe-up recovery: FAST after an isolated shrink, SLOW after a storm,
  floor is not a special case;
* FAST-unreachable pin (§6).

Run: `uv run pytest tests/test_aimd_free_lane_policy.py -q` → `12 passed`.

Suite health note (19/10 20:0x): the full suite at that moment reported
failures confined to `tests/test_free_budget.py`, an in-progress sibling
slice; every failure was in files this slice does not own, and the
AIMD/limiter tests (`test_limiter.py`, `test_pacing.py`, this file) were
green. Re-run before merge rather than trusting this note.

## 8. Open items for the integrator (not this slice)

1. **Zen gate floor is 1** — same configuration that collapsed go at 18:57.
   One-line mitigation: the same `98-aimd-floor2.conf` drop-in, or better,
   an `aimdMin` in the Nix module (see item 3).
2. **Ceiling 1024 vs mission "never cross the line"** — see §4.
3. **The floor is applied by transient drop-ins, not by Nix.** `mkEnvAttrs`
   already threads `aimdMin`, but `opencode-throttle-proxy.nix` never passes
   it, so the floor is a hand-written file in
   `~/.config/systemd/user/…service.d/` that a host rebuild does not own and
   a second host will not get. `aimdMin` belongs in the module next to
   `aimdInitialConcurrent` (which is already passed).
4. **Recovery from the floor is slow on long generations** (§6): at floor 2
   with 200 s generations the ladder buys back one slot per ~28 min of clean
   traffic. If that is too slow, the lever is the FLOOR (start higher, §2),
   not `AIMD_RAMP_AFTER` — lowering the ramp threshold trades the same
   429 wall for faster re-approach. Spec 334's floor-dwell ramp is the other
   answer and does not change this section's arithmetic.

## 9. Verify this document

```sh
# live gate env (§3) — merged, not per-file
for u in opencode-go-throttle-proxy opencode-zen-throttle-proxy; do
  systemctl --user show "$u.service" -p Environment --value | tr ' ' '\n' |
    grep -E 'AIMD|QUEUE_MAX_WAIT|CLAUDE_API_THROTTLE_MAX'
done
# live limiter state (§3)
curl -fsS http://127.0.0.1:8770/__throttle/health | jq '.bearers.e574f38a.limiter'
# sizing table (§2)
uv run python docs/evidence/aimd-free-lane-floor-sizing.py
# tests (§7)
uv run pytest tests/test_aimd_free_lane_policy.py -q
```
