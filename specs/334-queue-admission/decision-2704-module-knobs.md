# Lead decision — slice 2704 module knobs (NixOS)

**Decided:** 19/10/2026 19:46 BRT, queue-admission lead.
**Scope:** the two follow-ups handed to the lead. No commit, no host change.

## Verification performed

- Worktree `~/NixOS-2704-throttler-module-knobs`, branch
  `2704-throttler-module-knobs` @ `ea432b1c` — present as reported.
- `git diff --stat origin/main -- modules/` → 3 files, +97/−10
  (`mimo-throttle-proxy.nix`, `opencode-throttle-proxy.nix`,
  `throttler-gateway.nix`), and the diff does declare `lib.mkOption` for
  `maxHoldRetryAfterS` / `aimdMin` / `queueMaxWaitS` and wires
  `inherit (cfg) … aimdMin …` into `mkEnvAttrs`. **Options, not literals —
  confirmed.** `anthropic-throttle-proxy.nix` untouched, as reported.
- Live drop-ins inspected (the module's supposed-to-become-inert patches):
  `99-hold240.conf` on go/zen/mimo, `98-aimd-floor2.conf` on go,
  **plus `99-max-concurrent.conf` on go AND zen — which the report does not
  mention at all.**

## 1. zen `aimdMin = 2` — **ACCEPT**

Same free catalog, same upstream family (`opencode.ai/inference{,/go}`), same
shared account (`e574f38a`, live on both gates), same client contract
(`queue_max_wait=180`, pi idle 300). On a lane whose generations measured
171-303 s, a floor of 1 is a 503 factory by arithmetic, not by bad luck: a
single slot's drain estimate is its own service time, so *any* queued depth
pushes `est_wait` past 180 s and the arrival is refused. That is precisely what
the go gate did for 15.2 min at `max_concurrent=1` (spec 334 §1 receipts).

Floors should match across twin lanes. **Accept**, with the note that spec 334's
floor-dwell ramp makes the floor far less lethal than it was at incident time —
it is now a width floor, not a recovery gate — so this change is low-risk and
the two must be shipped as a coherent pair.

`throttlerGateway` (`:8765`) keeping `aimdMin = 1` is **also correct** — leave
it. The anthropic lane has real unified budget headers and a different workload;
it must not inherit the free-lane floor.

## 2. Drop-in deletion — **NARROWED: reject the blanket list**

The report says the five drop-ins "can be deleted after the next switch". That
is true for `99-hold240.conf` (×3) and `98-aimd-floor2.conf` (×1) — both are now
module options with matching defaults. It is **not** true for the unlisted
`99-max-concurrent.conf`:

```
$ cat ~/.config/systemd/user/opencode-{go,zen}-throttle-proxy.service.d/99-max-concurrent.conf
[Service]
Environment=CLAUDE_API_THROTTLE_MAX=1024
```

The module's `maxConcurrent` default is **12** (and it renders
`aimdInitialConcurrent = cfg.maxConcurrent`, so a restart would also warm-start
at 12). Deleting that drop-in therefore reverts **both free gates from hard_max
1024 → 12**, silently.

That is the wrong direction for this mission, and the evidence says so:

- live `:8770` at 19:35:29 ran bearer `e574f38a` at **`max_concurrent=36`**,
  `inflight=13`, window clear, zero client-visible 429 — AIMD itself discovered
  a clean width above the module default;
- the module's own comment records 32-wide bursts clean (17/10);
- `grow()` only ramps *below* `hard_max`, so a 12 ceiling makes 36
  unreachable for the rest of the lane's life.

**Ruling:**
1. Delete ONLY the three `99-hold240.conf` and the one `98-aimd-floor2.conf`
   after the switch. `zzz-subscription-cleanup-341.conf` (throttler-gateway) is
   unrelated to this slice — leave it.
2. **Keep `99-max-concurrent.conf`** until `maxConcurrent` for the free gates is
   re-measured and raised. Recommended: set the free-gate default to **32**
   (the measured-clean burst width, leaving the AIMD ceiling headroom to 1024
   only where an operator wants it), then retire the drop-in.
3. Order matters: land the `maxConcurrent` change **first or in the same switch**
   as the deletion. Deleting before the option matches the live value is a
   silent cap regression on two production gates.

## 3. Hand-off

- The 2704 slice owns the NixOS-side follow-through; the lead's Python slice
  (spec 334) does not touch it.
- Spec 334 preserves the behaviours this module depends on: `AIMD_MIN` is still
  the shrink floor (`FairBearerLimiter.aimd_floor()`), and
  `MAX_HOLD_RETRY_AFTER_S=240` still bounds the hold path — hold=240 stays the
  contract, never pinned lower here.
