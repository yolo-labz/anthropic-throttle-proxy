# Slice A review — two policy calls + three corrections

**Lead:** queue-admission (p1H). **Decided:** 19/10/2026 23:45 BRT.
**Source:** `/tmp/aimd-slice-report.md` (AIMD policy / limiter.py slice).

---

## 1. Urgent item — zen floor: **ALREADY RESOLVED, no action taken**

The report says zen `:8774` is at floor 1 with no drop-in. Verified live at
23:35 — **it now has one**, and this was already true before I read the report:

```
~/.config/systemd/user/opencode-zen-throttle-proxy.service.d/
    98-aimd-floor2.conf   <-- present
    99-hold240.conf
    99-max-concurrent.conf
systemctl --user show opencode-zen-throttle-proxy -p Environment
    THROTTLE_AIMD_MIN=2   <-- merged env, live
```

Both free gates now floor at 2. The report's finding was stale on arrival.
**Did not duplicate the drop-in or restart anything.**

---

## 2. Ceiling policy call — **64 on both free gates. APPLIED.**

`CLAUDE_API_THROTTLE_MAX` / `hard_max` was **1024** on both (via
`99-max-concurrent.conf`, 11:29), not the Nix-declared 12.

### Why not R1's 32

R1 measured a *32-wide burst* clean on 17/10. A burst measurement is not a
sustained-width limit, and the report's own R3 gives the number that matters:
**`max_concurrent` was 1024 all day and actual `inflight` peaked at 18.**
Twice over, 32 is a bad line:

- It is only **1.8×** the observed peak of 18. Queue depth under a
  generation (171–303 s) longer than the wait budget (180 s) is **0–1**
  (report item 4), so there is almost nothing to absorb a spike — a modest
  wake-up would shed immediately into the 503/retry loop that killed a pane
  at 19:02.
- Cutting the ceiling **does not prevent a single 429 on these lanes.** The
  free-tier research measured the bare 429s landing at **inflight 0–2**, with
  a *paid* Go model returning 200 in the same minute, and free models do not
  count toward budgets at all. Those 429s are a rate/usage machine, not a
  width signal. Trading 503 storms for 429s we were never going to avoid is
  strictly worse.

### Why not 1024

1024 is not a safety stop, it is the absence of one (report R5: `hard_max` is
AIMD's only structural stop). AIMD is reactive — it learns the line by
crossing it, which is exactly what "queue, never cross the line" forbids.

### Why 64

- **3.5× the measured all-day peak (18)** and **16× tighter than 1024**, so a
  runaway wake-storm is bounded instead of unbounded.
- **Never binds today.** At the operating point, `inflight` is 18–21.
- The genuine line on these lanes is **rate**, and rate is not the ceiling's
  job. See §5.

### Applied (hot-retuned, no restart, no dropped turns)

`POST /ui/config key=max_concurrent value=64` on both gates, 23:38:37:

```
:8770  cap 162 -> 64   hard_max 1024 -> 64   inflight 21 (before AND after)  served 2101 -> 2103
:8774  cap   8 -> 8    hard_max 1024 -> 64   inflight 0
journal: bearer-retune bid=e574f38a hard_max=64 max_concurrent=64   (both gates)
         no queue-wait-timeout, no status=429/503 within the window
config_override_drift: {'max_concurrent': {'override': 64, 'env': 1024, 'env_known': True}}
```

Hot-retune was chosen over a drop-in precisely because a restart would have
killed the 21 turns in flight. **Revert:** `POST /ui/config/reset
key=max_concurrent`, or re-deploy the module.

---

## 3. Floor call — **raise the default to 6 (R2), owned by the Nix module**

R1 says keep 2, R2 says 6. **I take 6**, on the report's own arithmetic (item 4):
when generation > wait budget the queue carries zero depth, so **the floor *is*
the admission width**. Floor 2 therefore admits exactly two panes per
generation and 503s every sibling — which is not hypothetical, it is the
17:57 cluster that happened *at* `mc=2`.

Against that, the cost of 6 during pushback is bounded: dispatch is already
gated by the 30 s synthetic pause, and the 429s are not width-driven (§2). So 6
buys back admission width at no measured rate risk.

**Not applied as a transient drop-in** — item 3 of the report is the real
defect: `mkEnvAttrs` threads `aimdMin` but `opencode-throttle-proxy.nix` never
passes it, so the floor is owned by drop-ins, not Nix. Adding a *third*
transient patch would deepen exactly the drift that finding identifies. The 2704
module-knobs slice owns `aimdMin` (I already accepted its zen `aimdMin=2`
proposal); this is the value it should default to, together with `maxConcurrent`
= 64 above.

---

## 4. Correction — item 5's "2.8 h recovery" is a property of the DEPLOYED build

That figure is `RAMP_AFTER(10) × slots-to-regain × generation / floor`, which
holds for the code as deployed. **This worktree already changes it**: spec 334's
floor-dwell ramp (`limiter._may_grow`) lets a lane that has *rested* at the
floor regain one slot per `2 × AIMD_BACKOFF_S` dwell + one clean success,
instead of ten successes at the collapsed width.

It does **not** contradict `test_bearer_at_floor_recovers_on_the_slow_ramp` —
that pin elapses only `BACKOFF + 1` (31 s), which is under the 60 s dwell, so a
storm floor still ramps conservatively. Real collapses are quiet at the floor,
so they take the fast path. Recovery is still generations, not seconds — but
~6× better than 2.8 h, and the report's number should not be quoted against
merged code.

---

## 5. Where the fleet's remaining effort should go

Both §2 and the free-tier research point the same way: **width is not the line;
rate is.** A concurrency ceiling cannot prevent a 429 that the upstream
observed at inflight 0–2. The lever that actually implements "never cross the
parallel-consumption line" is the **rate/quota pre-check at admission** — the
one admission decision (AIMD ceiling + meter/quota + retry-after state agreeing
*before* dispatch) that was this slice's original target and is still open.
The ceiling is now a bounded safety stop; it is not, and cannot be, the guard.
