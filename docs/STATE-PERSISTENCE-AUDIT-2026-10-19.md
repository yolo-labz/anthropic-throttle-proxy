# State-persistence audit — retry-after-state.json + budget-window state (19/10/2026)

Slice: budget/state-persistence (orchestrated multi-slice hardening of the
free-model gates). Scope: crash-safety and cross-restart correctness of
`retry-after-state.json` and the budget-window state files under
`~/.local/state/<gate>/`, plus the reservation ledger's snapshot contract.
Every claim below carries a receipt; every fix carries a test in
`tests/test_state_persistence_hardening.py` (23 tests, green).

## TL;DR — the three questions the slice asked

| Question | Answer |
|---|---|
| Do restarts lose pushback state? | **YES on the go/zen gates — persistence was never wired there.** `THROTTLE_RETRY_AFTER_STATE_FILE` is opt-in with an empty default; only the anthropic gate unit sets it. The 18:48:57 restart forgot every noted window and re-descended the AIMD ladder with real 429s. **Fixed**: the knob now defaults to the gate's own XDG state dir (zero-config), so every gate persists on its next activation with no unit change. |
| Stale entries? | **YES on disk.** Expired entries were pruned from memory on every load but never rewritten to disk (measured: a **39.4-day-old** entry still in the anthropic gate's file). Fixed: load now self-heals the file once per process start. |
| Clock-skew handling? | **Partially.** Restore-cap (900 s) already bounded backward jumps across restarts. But non-finite deadlines (`NaN`/`Infinity`, accepted by Python's `json.loads`) crashed the request path, and the ledger's own documented ponytail (future `at` holds debt forever) was unguarded. Both fixed; the FORWARD-jump residual remains and is now pinned by test + documented. |

## Findings (audit, pre-patch) with receipts

### F1 — CRITICAL: go (:8770) and zen (:8774) gates persist nothing

* `systemctl --user cat opencode-go-throttle-proxy.service` /
  `...-zen-...`: both units set only `XDG_STATE_HOME=...`; **no
  `THROTTLE_RETRY_AFTER_STATE_FILE`** (the anthropic gate's unit does set it).
* `ls -la ~/.local/state/opencode-throttle-proxy/` and
  `.../opencode-zen-throttle-proxy/`: **empty directories** — no state file
  was ever written.
* Journal (`journalctl --user -u opencode-go-throttle-proxy`, 19/10):
  pre-restart PID 2457560 had shrunk the shared bearer `e574f38a` to
  `max_concurrent=1` (18:48:19). `Stopped`/`Started` 18:48:57 (new PID
  2599264). The new process re-discovered the same 429s from scratch:
  18:49:02 `max_concurrent=8` → 18:50:24 `5` → 18:51:44 `3` → 18:53:15 `2` →
  18:53:48 `1` — **five live 429s against the shared upstream** re-deriving
  what the old process already knew, followed by the 19:02–19:06
  `pre-queue-depth` 503 storm (`est_wait 347–355 s > max_wait 180 s`) that
  killed the pi panes.
* Already flagged as open item §8.3 in `docs/FREE-LANE-RUNBOOK.md` (zai
  variant: env points at a path whose file is absent).

**Root cause**: the persistence module worked (covered by
`tests/test_proxy_helpers.py::test_retry_after_state_restored_for_new_limiter`)
but was wired OFF for both opencode gates by an opt-in-with-empty-default knob.

### F2 — HIGH: one corrupt state file crashes the whole request path

* Python's `json.loads` accepts the non-standard literals `NaN`/`Infinity`.
  The load filter passed both (`nan <= now` is False; `nan > now+cap` is
  False, so the restore cap never clamped), and the crash landed in the
  `int(...)` of the cap **log line inside the load itself** (reproduced:
  `OverflowError: cannot convert float infinity to integer`) — and in
  `_restore_retry_after` via `ValueError('cannot convert float NaN to
  integer')`. Since the module-global cache stayed unset on raise, **every
  later caller re-raised**: `proxy._bearer_retry_after_remaining`
  (proxy.py:881) and `accounts._retry_after_remaining_for_token` /
  `_maybe_clear_stale_retry_after` (accounts.py:892/915) — one poison value
  took down every retry-after decision in the process.
* `note_retry_after(inf)` (a malformed `Retry-After: Infinity` header parses
  via `float()`) latched an infinite wall window AND persisted an
  `Infinity` literal; `wait_retry_after` would then `asyncio.sleep(inf)` —
  a hang no window can end.

### F3 — MEDIUM: stale entries live on disk forever

* `cat ~/.local/state/anthropic-throttle-proxy/retry-after-state.json` →
  `{"666a53af": 1789043416.2060034}`; `date +%s` → `1792448605` →
  expired **≈39.4 days earlier**. Loads pruned it from memory and never
  rewrote the file (the rewrite ran only when a cap fired). Harmless at
  runtime, misleading in incident forensics.

### F4 — MEDIUM: the retry-after writer had no durability and a shared tmp name

* `_persist_retry_after_state` wrote `write_text` + `os.replace` with a
  **fixed** `<file>.tmp` name and **no fsync** — while the ledger's save
  path does tmp + file fsync + replace + **directory fsync**. A power loss
  right after the replace can persist an empty file (delayed-allocation
  rename hole): the next boot silently forgets every window — the exact
  cold start the feature exists to prevent. Two writers sharing one file
  shared one tmp path.

### F5 — ledger: backward clock jump holds budget debt forever (documented ponytail, now closed)

* `ledger.py` docstring (pre-patch): *"ponytail: persisted wall-clock
  windows assume a trustworthy clock; backward jumps can hold debt longer
  than 60 s…"*. Pruning keeps any entry whose `at` is in the future
  (`now - at < WINDOW_S` is true for negative ages), so a backward jump
  between save and restore — or a corrupt hand-edited far-future `at`
  (e.g. `1e18`) — held the debt until the wall clock caught up: **forever**,
  permanently blocking a `max_requests=1` lane. The ledger is default-off
  (spec 245 T002, not hot-path-wired), so this was latent, not live.

### Non-findings (verified safe, documented rather than changed)

* **Backward jump across restart (retry-after)**: already bounded by
  `RETRY_AFTER_RESTORE_CAP_S=900`. No change needed.
* **Forward jump**: expires windows/debt early → the next real 429 re-notes
  (self-correcting for the limiter). For the ledger the residual is
  structural (a wall-clock snapshot cannot distinguish "old" from "clock
  moved") — pinned by `test_forward_clock_jump_expires_debt_early_is_pinned`;
  the ledger stays default-off until a calibrated clock policy lands.
* **AIMD live ceiling is not persisted** (by design): a restart re-earns
  width from `THROTTLE_AIMD_INITIAL_CONCURRENT` (runbook §5.4(b) uses this
  deliberately as the manual reset lever). Persisting the ceiling would risk
  a stale-low width and would remove that lever — **decision for the lead,
  not implemented**. With F1 fixed, the restored window + re-armed probe
  gate suppresses most of the rediscovery cost anyway: the first dispatch
  waits out the window instead of burning 429s down the ladder.
* **Endpoint cache** (`accounts.py`, budget-window *display* state):
  same write pattern (fixed tmp, no fsync) and a NaN `fetched` would look
  "infinitely fresh" — display-only, **out of this slice's file scope**;
  flagged below as follow-up.

## Patch (uncommitted, worktree, per slice rules)

| File | Change |
|---|---|
| `src/anthropic_throttle_proxy/config.py` | `_resolve_retry_after_state_file()`: explicit path wins; `off`/`none`/`disabled`/set-but-empty disables (legacy meaning preserved); **unset → `$XDG_STATE_HOME/anthropic-throttle-proxy/retry-after-state.json`** (fallback `~/.local/state/...`), mirroring `ENDPOINT_CACHE_FILE`. Each gate already scopes its own `XDG_STATE_HOME`, so go/zen get per-instance files with no unit change. |
| `src/anthropic_throttle_proxy/limiter.py` | Load: rejects non-finite values at the trust boundary; logs corrupt-JSON loads (FileNotFound stays silent = normal cold start); **self-heals the file** when capped OR expired OR poison entries were dropped. Persist: `tempfile.mkstemp` unique tmp + `allow_nan=False` + file fsync + `os.replace` + directory fsync (durability parity with the ledger); never-raises contract kept. `note_retry_after`: ignores non-finite input (matches `_parse_retry_after`'s treatment of unparseable input). |
| `src/anthropic_throttle_proxy/ledger.py` | Restore clamps entries with future `at` to the restore-time now (logged) — bounds a backward jump/corrupt snapshot to one window; module docstring updated (forward-jump residual documented). |
| `tests/conftest.py` | Autouse fixture isolating `config.RETRY_AFTER_STATE_FILE` to a per-test tmp file and forcing the module-global cache to `None` — mandatory now that persistence defaults ON (otherwise the suite would read/mutate the operator's real state file). |
| `tests/test_state_persistence_hardening.py` | **New**, 23 tests: NaN/Infinity rejection (load, restore, request path, note), stale sweep, config resolution (default/opt-out/path-wins), fsync + unique tmp + never-raises, cross-restart round trip, ledger clamp + pinned forward-jump residual. |

## Test evidence

* Baseline (pre-patch): `tests/test_limiter.py tests/test_ledger.py` → **119 passed**; retry subset of `test_proxy_helpers.py` → **20 passed**.
* Red phase (tests first): `tests/test_state_persistence_hardening.py` → **20 failed, 3 passed** (the 3 = pre-existing correct behavior pins).
* Post-patch: hardening file → **23 passed**; targeted suites → green;
  **full suite → 2341 passed, 0 failed** (116.9 s); `ruff check` on all touched files → clean.
* Stability: `tests/test_retry_after_wait.py` × 5 consecutive runs → 6/6 green each.

## Deployment + follow-ups for the lead

1. **Takes effect on next activation** (units run the nix-store binary;
   worktree left uncommitted per slice rules). No unit changes required —
   the XDG default picks up each gate's own `XDG_STATE_HOME`. Go gate file
   will be `~/.local/state/opencode-throttle-proxy/anthropic-throttle-proxy/retry-after-state.json`.
   Optional: pin the path explicitly in the units instead of relying on the default.
2. **Doc drift to update when this lands**: `README.md` env table (add the
   new default), `docs/FREE-LANE-RUNBOOK.md` §5.4(b)(ii) ("on go/zen there
   is no `THROTTLE_RETRY_AFTER_STATE_FILE`") and §8.3 open items #2/#3.
3. **Out-of-slice follow-ups** (not done, not my files):
   * `accounts.py` `_persist_endpoint_cache`/`_load_endpoint_cache`: same
     tmp/fsync hardening + non-finite `fetched` rejection.
   * Decide on persisting the AIMD live ceiling (see non-findings).
4. **Not committed** — worktree changes only. My slice touches exactly the
   four files + one new test file listed above; a sibling session has
   staged changes to `limiter.py`/`metrics.py`/`pyproject.toml` in the index
   (worktree content of `limiter.py` was identical to HEAD before my edits;
   my edits are worktree-only, so their staged state is untouched).
