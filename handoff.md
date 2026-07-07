# Handoff: throttle incidents

This handoff is for the next Claude Code session touching this repository.
Read it before changing throttle behavior, central deployment, Nix pins, or
host activation. Latest incident first.

---

## 04-05/07/2026 - Account-routing series shipped; 7d budget brake stays on

### What shipped (all merged, CI + CodeQL + Sonar green at `f984c18`)

- **#76** — 502 responses no longer echo upstream exception detail.
- **#77** — opt-in local account routing (`THROTTLE_ACCOUNT_ROUTING=least_loaded`
  + `THROTTLE_ACCOUNT_CRED_PATHS`): the proxy picks the least-loaded usable
  credential per `POST /v1/messages` and rewrites only the upstream
  `Authorization` header. Also defaulted `THROTTLE_PRIORITY_RESERVE_SLOTS` to 0
  and added the fake-Anthropic simulator + k6 workload docs.
- **#78** — routing excludes `allowed_warning`/pressured accounts; queued
  requests re-check long `Retry-After` after getting a slot and fast-fail
  instead of sleeping in the only slot.
- **#79** — routing also consults cached per-endpoint 429/`rejected` state
  (`_fresh_endpoint_entry` in `accounts.py`).
- NixOS twins: #1141 (routing env in the HM module) → #1143 → #1144
  (pin `rev = f984c18`), all merged in `~/NixOS` main.

### Deployment state (verified 04/07 ~23:30 -03)

- **Desktop**: HM activation deferred (niri host), so the base unit still
  points at a pre-#79 build. Bridged by the persistent drop-in
  `~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-account-routing-hotfix.conf`
  (cred paths + `least_loaded` + `ExecStart` pinned to the #79 store path,
  symbol-verified) and the gcroot
  `~/.local/state/anthropic-throttle-proxy/live-warning-fallback-package`.
  Reboot-safe. The next `nixos-smart-switch` / `nh os boot` + reboot makes the
  base unit current; do NOT run a live switch mid-storm — it restarts this
  user service and kills in-flight Claude streams.
- **Central Dokku**: was 4 PRs behind (container built 03/07 right after #74,
  so the #76 leak fix was not live). Redeployed 04/07 23:32 via
  `git push dokku main` (`a23e584..f984c18`), zero-downtime checks passed,
  root probe 200 in 3 ms, local `central_status=up`. Central keeps routing
  OFF by design (no credential files there — per #77 deploy notes).
- Both credential files alive and DISTINCT accounts again (B was re-logged
  after the 03/07 refresh-token wipe; utils differ, so routing is
  live-effective).

### The budget brake (do not loosen without redoing this math)

`overrides.json` still carries the 03/07 storm clamp: `max_concurrent=1`,
`central_local_max_concurrent=1`, `min_dispatch_gap_ms=500`, `ramp_after=20`,
`decrease=0.6`. Overrides WIN over env on restart.

04/07 23:30 evidence: active account 5h=0.30 but **7d=0.69 at only ~24% of the
7d window elapsed** (window started 03/07 09:20 -03) — a burn pace of ~2.8×
sustainable, projecting a 7d wall within ~1 day. The other account already sits
at 7d=0.99 and is auto-excluded by #78. A 7d `rejected` means a **days-long**
outage, unlike a 5h wall. So the clamp is doing exactly its job; the visible
cost is client disconnects climbing (tabs time out while queued; #68 drops
them pre-dispatch so no quota is burned). Loosening the clamp trades a
days-long fleet outage tomorrow for tab latency today — don't.

Relax criteria: BOTH accounts' 7d below ~0.8 AND burn pace ≤ ~1.15×
(`util_7d ÷ elapsed-window-fraction`). Then restore
`central_local_max_concurrent=2` and `min_dispatch_gap_ms=50..100` via
`POST /ui/config` (hot, persists to overrides.json).

### Priority lane enabled (05/07)

Set `priority_reserve_slots=1` via `/ui/config` (persisted). Short no-tools
calls (≤8192 max_tokens, ≤256 KB body) — Stop-hook evaluators, titles — now
dispatch from the dedicated slot instead of starving behind long Opus/Fable
generations at 1-slot concurrency. Negligible 7d burn; total upstream
concurrency is now ≤ live cap (1) + reserve (1).

Known observability gap: the priority lane exposes no counters in
`/__throttle/health` or `/metrics` — you cannot yet distinguish lane
dispatches from main-pool dispatches without reading the queue logs. Add
gauges/counters if the lane's behavior comes into question.

## 30/06/2026 - OpenCode z.ai Coding Plan throttle split

### What changed

OpenCode desktop tabs now share one z.ai Coding Plan bearer via the throttle
proxy. z.ai sends at least two distinct 429 classes:

- `1302`: concurrent-request pushback. This remains an AIMD signal and shrinks
  the live per-bearer ceiling.
- `1308` / `1316` / `1317`: plan quota windows. These are quota gates, not
  evidence that concurrency is too high. z.ai puts reset data in the JSON body
  rather than a `Retry-After` header, so the proxy parses `reset_time` /
  `resetAt` variants and the older `message` text into a local retry-after
  window.

Default resume jitter is `THROTTLE_ZAI_QUOTA_RESET_JITTER_S=15` seconds so all
tabs sharing one key do not resume at the exact reset second.

### Local endpoint contract

For the desktop OpenCode client, run a dedicated local instance with:

```sh
THROTTLE_UPSTREAM=https://api.z.ai/api/coding/paas/v4
CLAUDE_API_THROTTLE_MAX=2
THROTTLE_AIMD_INITIAL_CONCURRENT=2
THROTTLE_QUEUE_MODE=fair
THROTTLE_HOST=127.0.0.1
THROTTLE_PORT=8766
```

Then set OpenCode `provider.zai-coding-plan.options.baseURL` to
`http://127.0.0.1:8766`.

---

## 29/06/2026 - Fleet Claude credential sync to active usable account

### What was wrong

Pedro asked to update Claude credentials across the other hosts, ProxMox, and
WSL surfaces. The local account picker showed the active usable account was
account A:

    A(fd332a33): 5h=31% 7d=7% pace=1.75x | B(a8060655,use=1): 5h=31% 7d=7% pace=1.75x | pick=a

The active credential file was `~/.claude/.credentials.json`, bearer
`fd332a33`, expiring `29/06/2026 18:08 -03`. Pre-sync probes found drift on
several reachable hosts:

- `DT-SCI018` / Pearson WSL was already current: `fd332a33`.
- `ProxMox` had stale bearer `cd8d8f33`.
- `ProxMox.Dokku` had stale bearer `c3e6a625`.
- `Nix.Server` and the alternate `ProxMox.NixOS` route had stale bearer
  `9a3192ff`.
- `ProxMox.Runner` had stale bearer `db5a325b`.
- `Mac.Pro` had Claude installed but no `~/.claude/.credentials.json`.

Unreachable or intentionally skipped during this pass:

- `Mac.Air`: SSH failed with `Permission denied (publickey,password)`.
- `LAN.ProxMox` root alias: local `/home/notroot/.ssh/id_rsa` was missing and
  root SSH failed public-key auth.
- `Pi`: no Claude binary and no credential file.
- Several LAN-only ProxMox VM aliases were unreachable from the current network
  (`No route to host`) or denied the configured key.

### What was repaired

- Synced the active A credential (`fd332a33`) to:
  - `ProxMox`
  - `ProxMox.Dokku`
  - `Nix.Server`
  - `ProxMox.Runner`
  - `Mac.Pro`
- Preserved each target's existing credential/config as timestamped backups
  before writing:
  - `~/.claude/.credentials.json.bak-<timestamp>`
  - `~/.claude.json.bak-<timestamp>`
- Updated each target's `~/.claude.json` `oauthAccount` metadata to match the
  active account (`pedrobalbino@proton.me`) while preserving the rest of the
  target config.
- Re-ran `pearson-claude-token-sync`; the Pearson WSL credential stayed on
  `fd332a33`.
- `ProxMox.Runner` initially could not accept the upload because `/` was at
  `100%` with `0` available blocks. Before syncing it, freed only unopened
  runner temp/cache artifacts:

      /tmp/.claude-active-cred.json
      /tmp/gitleaks.tar.gz
      /home/notroot/actions-runner-pidashboard-vm103/_work/_temp/79c4df5d-6be0-44f7-8bed-bdee77afda9a/cache.tzst
      /home/notroot/actions-runner-piorchestrator-vm103/_work/_temp/fd88bff9-b579-4cc3-a6c5-cacaf2ca6c0e/cache.tzst

  There was no active `Runner.Worker`, and `fuser` reported no users for the
  two `cache.tzst` files. Free space moved from `0` to `3.1G`; after sync the
  runner still had `2.3G` free.

### Final verification

- `claude-account-pick status` still selected A (`fd332a33`), with 5h `31%`
  and 7d `7%`.
- Local proxy health after the sync:

      central=up queued=0 active=fd332a33 status=allowed status_7d=allowed util5=0.33 util7=0.07 retry_after=null

- Post-sync probes showed all reachable targets on bearer `fd332a33`:
  - `DT-SCI018`
  - `ProxMox`
  - `ProxMox.Dokku`
  - `Nix.Server`
  - `ProxMox.NixOS`
  - `ProxMox.Runner`
  - `Mac.Pro`
- Claude smoke tests passed on hosts where Claude is installed:

      DT-SCI018 smoke=ok output=CRED_OK
      Nix.Server smoke=ok output=CRED_OK
      Mac.Pro smoke=ok output=CRED_OK

Rollback for any synced host is to restore the host-local timestamped backup
created during this pass, then rerun the credential probe and a Claude smoke
test if that host has Claude installed.

---

## 27/06/2026 - Zellij fleet recovery and Pearson remote-token drift

### What was wrong

Pedro reported that some Pearson and DeliCasa zellij tabs still had problems
after the server/gateway repair. The host-side zellij sessions were not live:
`DeliCasa` and `Pearson` were saved as exited sessions, while `HOME` was the
current session. Historical pane dumps showed the affected Pearson panes had
the managed credential nudge:

    Please run /login · API Error: 401 throttle-proxy: active account changed; re-read credentials

The local proxy was healthy (`central=up`, `queued=0`, egress OK), so this was
not a queue/concurrency incident. Pearson had an additional remote-token drift:
the corp WSL box's `~/.claude/.credentials.json` hashed to bearer `a3ad80d7`,
which matched neither the local active bearer (`3724b5ae`) nor the local
standby bearer (`0e424d0a`) at the time. The recurring
`pearson-claude-token-sync` helper was also unsafe for the new account model
because it hardcoded `~/.claude-b/.credentials.json`, even though account B was
then exhausted/rejected and account A was the usable active slot.

### What was repaired

- Backed up and synced the Pearson corp WSL credential to the current active
  local account, then synced the non-secret `oauthAccount` metadata block.
  Verification on the corp box showed bearer `3724b5ae` and a future
  `expiresAt`.
- Ran a Pearson-side Claude smoke test:

      claude -p "Reply PEARSON_OK only." -> PEARSON_OK

- Patched `/home/notroot/.local/bin/pearson-claude-token-sync` so future runs
  default to the active desktop credential:

      SRC="${PEARSON_CLAUDE_TOKEN_SRC:-$HOME/.claude/.credentials.json}"

  `PEARSON_CLAUDE_TOKEN_SRC` remains an explicit override for intentional
  one-off syncs.
- Restarted the user timer. Final state:
  `pearson-claude-token-sync.timer` was `active (waiting)` with the next trigger
  scheduled for `27/06/2026 07:01 -03`.
- Resurrected the saved `Pearson` and `DeliCasa` zellij sessions with
  `zellij attach --force-run-commands`, then closed only the temporary attach
  clients. No broad `pkill`, `killall`, proxy restart, or
  `claude-account-promote --now` was used.
- Sent targeted resume prompts only to the panes that needed work:
  - Pearson `terminal_1` / `🛒 Ecom`: resume the failed 26/06/2026 ecommerce
    meeting/vault update task.
  - Pearson `terminal_2` / `🔷 Clever`: resume the failed 26/06/2026 Clever
    meeting/vault update task.
  - DeliCasa `terminal_0` / `🏠 Root`: resume fleet close-out coordination.
  - DeliCasa `terminal_6` / `📊 PiDash`: resume the PiDashboard blocker with
    evidence-first CI/local repro work.
- Left the other visible panes alone because scanners reported no stuck API
  banner:
  - Pearson Root and Gauge.
  - DeliCasa NextClient, BridgeServer, Wire, ESP32, and PiOrch.

### Final verification

- Host-side `zellij list-sessions` showed `HOME`, `DeliCasa`, and `Pearson`
  live.
- `NUDGE_SESSION=Pearson claude-tabs-nudge --all` ->
  `scanned=3 claude=3 stuck=0`.
- `NUDGE_SESSION=DeliCasa claude-tabs-nudge --all` ->
  `scanned=6 claude=6 stuck=0`.
- `CYCLE_SESSION=DeliCasa claude-tabs-cycle --dry-run` ->
  `scanned=6 stuck-cyclable=0`.
- Pane dumps after the targeted prompts showed no residual
  `Please run /login` / `401 throttle-proxy` banner:
  - Pearson Ecom returned to a live prompt after completing its check
    (`Cooked for 41s`).
  - Pearson Clever returned to a live prompt after its vault update/check
    (`Worked for 1m 17s`).
  - DeliCasa Root was actively working the PiDashboard fleet blocker.
  - DeliCasa PiDash was actively running its CI/local repro shell.
- Local proxy health after the prompts: `central=up`, egress OK,
  `queued=0`, active bearer `3724b5ae` had no `retry_after`.

Rollback for the live Pearson token-sync helper patch is:

    sed -i 's#SRC="${PEARSON_CLAUDE_TOKEN_SRC:-$HOME/.claude/.credentials.json}"#SRC="$HOME/.claude-b/.credentials.json"#' ~/.local/bin/pearson-claude-token-sync

Only use that rollback if account B is intentionally the desired Pearson
source again; otherwise it reintroduces the stale-account failure mode.

---

## 27/06/2026 - Pearson tabs had stale Claude credentials after account collapse

### What was wrong

Pedro reported that the Pearson zellij session still had Claude Code access
errors after the tabs were restarted. The visible errors were:

    API Error: Server is temporarily limiting requests (not your usage limit) · upstream retry-after window is active; failing fast instead of holding the local gateway request
    Please run /login · API Error: 401 throttle-proxy: active account changed; re-read credentials

The proxy was not wedged. The live issue was local account identity state:

- `claude-account-pick status` reported both A and B as the same account
  (`id=0`) even though B's profile metadata still said
  `pedrobalbino@pm.me`.
- The live B credentials file hashed to the same OAuth bearer as A, so account
  switching was cosmetic and half of the fleet capacity was gone.
- Historical B backups had dead refresh tokens (`invalid_grant`), so restoring
  an old credentials file would only have recreated a broken login.
- `claude-tabs-cycle` correctly refused to treat all Pearson panes as
  cyclable; some were already at a shell or no longer showed a stuck banner in
  the scanner.

This was a credential-profile collapse, not a queue/concurrency bug in
`anthropic-throttle-proxy`.

### What was repaired

- Backed up the current B credential/profile files before changing them.
- Re-authenticated `~/.claude-b` as `pedrobalbino@pm.me` through a dedicated
  browser profile, instead of using `/login` inside a running Claude TUI.
- Verified `claude-account-pick status` returned distinct identities again:
  A stayed `id=0`; B returned `id=1`.
- Ran `pearson-claude-token-sync` and verified the remote Pearson WSL token
  hash matched the repaired local B bearer (`a3ad80d7`).
- Cleanly exited and resumed the affected Pearson project panes so they
  re-read the repaired credentials:
  - Ecom resumed `ac4ba3e7-fe6b-452b-9569-44d8b3a0f1f8`.
  - Clever resumed `f9fedeea-bd8c-411e-98f1-41819c602d27`.
- Left Root and Gauge untouched because they were not showing the old
  credential failure during verification.

### Systematic recovery path

Use this order when a future zellij/DC/Pearson tab reports
`active account changed`, `Please run /login`, or a long-window upstream
retry-after:

1. Read `/__throttle/health` first. If `inflight=0`, `queued=0`, and central is
   up, do not tune queue knobs.
2. Run `claude-account-pick status`. If A/B show the same identity id, do not
   cycle tabs yet; account switching is cosmetic until the credential files are
   repaired.
3. Back up the affected `~/.claude*` credential/profile files.
4. Reject dead backups by probing refresh-token validity before restore. An
   `invalid_grant` backup is not a recovery source.
5. Re-authenticate the collapsed profile in its own `CLAUDE_CONFIG_DIR` with a
   dedicated browser profile. Do not run interactive `/login` inside each
   project pane.
6. Verify `claude-account-pick status` shows distinct identities.
7. Run the project-specific token sync helper when the work happens in remote
   WSL/tmux/zellij state (`pearson-claude-token-sync` for Pearson).
8. For each affected Claude pane, prefer `/exit`, capture the resume id, launch
   the wrapper with `claude --resume <id>`, and send `continue`.

Falsifier for this diagnosis: if A/B identities are distinct and the active
credential hash matches the pane's bearer, then the failure is not credential
collapse; inspect upstream `Retry-After`, utilization windows, and the per-bearer
limiter instead.

---

## 24/06/2026 - DeliCasa tabs hit credential-swap 401

### What was wrong

After the local proxy/Nix activation gap was closed, account A (`4400c70e`)
remained under a long `Retry-After` until `26/06/2026 07:00 -03`. The active
credential was promoted to bearer `ae018561`, which made stale long-lived
Claude Code TUIs correctly receive:

    Please run /login · API Error: 401 throttle-proxy: active account changed; re-read credentials

The DeliCasa zellij session had been restored with 7 Claude panes, but those
panes still held the old in-memory token. The existing `claude-tabs-cycle`
automation did not recover them because:

- its API-error regex only matched banners that started directly with
  `API Error`, not the new `Please run /login · API Error` prefix.
- its scan loop incremented before dumping panes, so `terminal_0` was skipped.

Manual `/login` was the wrong recovery path: it opened the interactive account
login menu. The safe path was to cleanly `/exit`, capture the printed resume
token, relaunch via the wrapper with `claude --resume <uuid>`, and send
`continue`.

### What was repaired

- Promoted the standby credential with `claude-account-promote --now` at
  `24/06/2026 14:50 -03`. Active became `ae018561`; parked A stayed
  `4400c70e`.
- Recovered every DeliCasa Claude pane by clean exit/resume, preserving the
  prior sessions:
  - Root: `24ea66f0-dd3a-4ff4-b884-722041111fb7`
  - NextClient: `4a48a827-75f3-489a-a850-d0d127fb2f57`
  - BridgeServer: `9274bd67-3abf-4013-9d72-1d3e39096f80`
  - Wire: `962ea2f7-e503-49a5-85eb-90881aece030`
  - ESP32: `24877a0a-b38a-4e02-9b33-d7dbb834b0c5`
  - PiOrch: `e2bfc40e-7775-4047-9cde-554407317192`
  - PiDash: `0bbadd0e-d46a-4a90-8432-7c326d4ee8e8`
- Merged `phsb5321/NixOS#1032` at `24/06/2026 16:38 -03`
  (`ad4b5cbc45d3f911cb938120f7e27b1bdc2122df`):
  - `claude-tabs-nudge` and `claude-tabs-cycle` now classify
    `Please run /login · API Error ...` as a stuck credential-swap banner.
  - `claude-tabs-cycle` starts scanning at `terminal_0`.
  - regression coverage locks both cases.
- Activated desktop at `24/06/2026 16:40 -03`:
  `/nix/store/dl7h53pmfh5c2a51wzm0bv727wv7mjlk-nixos-system-desktop-26.11.20260616.3e41b24`.

### Final verification

- `python3 modules/home/claude-tabs-cycle.test.py` -> all cases pass.
- `git diff --check` -> clean.
- `nix eval .#nixosConfigurations.desktop.config.system.build.toplevel.drvPath --raw`
  -> succeeded.
- `nh os switch .` built and activated successfully; both
  `claude-tabs-cycle` and `claude-tabs-nudge` built in the activation graph.
- `CYCLE_SESSION=DeliCasa claude-tabs-cycle --dry-run` -> `scanned=6
  stuck-cyclable=0`, with the only visible stuck-banner pane skipped because it
  had active work. No keystrokes were sent.
- DeliCasa pane readback showed all 7 project tabs running `claude --resume`
  with the tokens listed above.
- Local proxy readback:
  - `anthropic-throttle-proxy.service`, `.socket`, and keepalive timer all
    `active`.
  - `/__throttle/health`: `queue=fair`, `central=up`, `inflight=0`, `queued=0`,
    `upstream_retries=0`.
  - active bearer `ae018561`: `served=504`, no `retry_after`, `util_5h=0.10`,
    `util_7d=0.97`, limiter max `12`.
  - parked bearer `4400c70e`: no served traffic, retry-after until
    `26/06/2026 07:00 -03`.

Rollback for the NixOS helper fix is a normal revert PR:
`git revert ad4b5cbc45d3f911cb938120f7e27b1bdc2122df`.

---

## 24/06/2026 - stale A-account tabs still rate-limited

### What was wrong

Pedro asked to check previous-session work and explain why Claude Code tabs
were still rate-limited.

Live proxy state was healthy, not queue-jammed:
`/__throttle/health` returned `inflight=0`, `queued=0`, `queue_mode=fair`,
`central_status=up`, and `upstream_egress_ok=true`. The live problem was
account state:

- then-current A credential hash (`~/.claude/.credentials.json`) was `ac454a4f`.
  That bearer was exhausted and held behind a long `Retry-After` until the
  7-day reset at `26/06/2026 07:00 -03`.
- then-current B credential hash (`~/.claude-b/.credentials.json`) was `324eadec`.
  That bearer was usable but already in `allowed_warning` with
  `util_7d=0.91` and reset at `29/06/2026 05:00 -03`.
- 39 `.claude-wrapped` processes were still running. Tabs still holding the A
  token in memory kept hitting bearer `ac454a4f`, so the proxy correctly
  returned the long-window fast-fail 429.

The previous proxy-side fix was merged in this repo as PR #59
(`741d1be`, 401 nudge for stale tabs), but the desktop service was still
running an older Nix package:

- systemd `ExecStart` pointed at
  `/nix/store/0zncmhx20a1mg3ckrfay1vxar3m5q1id-anthropic-throttle-proxy-0.1.0`.
- that store path's `_retry_after_fast_fail_response()` only returned a local
  429; it had no `credential-nudge` / `THROTTLE_ACTIVE_CRED_PATH` code.
- the live service environment had `THROTTLE_ACCOUNT_CRED_PATHS=...` but did
  not set `THROTTLE_ACTIVE_CRED_PATH`.

So the root cause was an integration/activation gap: source `main` contained
the #59 nudge, but the host package and service env did not. The limiter was
not malfunctioning.

### What was repaired

- Took over the previous NixOS integration PR:
  `phsb5321/NixOS#1025` ("single-active-account credential failover").
- Verified before merge:
  - `python3 modules/home/claude-account-promote.test.py` -> 9/9 passed.
  - `git diff --check` -> clean.
  - `nix eval .#nixosConfigurations.desktop.config.system.build.toplevel.drvPath --raw` -> succeeded.
  - `nix eval .#nixosConfigurations.server.config.system.build.toplevel.drvPath --raw` -> succeeded.
  - GitHub PR state was open, non-draft, mergeable/clean; GitGuardian passed;
    no review comments.
- Squash-merged NixOS PR #1025 at `24/06/2026 14:00 -03` as
  `685c06a5085a5fdcb97c9d58b30ed411586d0df7`, deleted the remote branch, and
  removed the local PR worktree/branch.

### Final closure

NixOS main first had only the integration, but the desktop host had not been
activated. The first readback still showed:

- `ExecStart=/nix/store/0zncmhx20a1mg3ckrfay1vxar3m5q1id-...`
- no `THROTTLE_ACTIVE_CRED_PATH` in the service environment
- local health still showing A `ac454a4f` under `Retry-After` and B
  `324eadec` usable.

Later readback on 24/06/2026 showed the on-disk credential files had rotated
again while stale bearer IDs remained in the live proxy process:

- `~/.claude/.credentials.json` hashed to `4400c70e`, expiring at
  `24/06/2026 22:00 -03`.
- `~/.claude-b/.credentials.json` hashed to `ae018561`, expiring at
  `24/06/2026 22:00 -03`.
- `/__throttle/health` still showed old bearer `ac454a4f` under the long
  `Retry-After`, old bearer `324eadec` in `allowed_warning`, and current B
  bearer `ae018561` serving requests.
- systemd still reported the old store path and no `THROTTLE_ACTIVE_CRED_PATH`.

Pedro then asked to solve everything. Follow-up work closed the activation and
package-pin gap:

- Merged `phsb5321/NixOS#1030` at `24/06/2026 14:19 -03`:
  `fable-tag` now requires `fable-tag <dir> <model>` and no malformed default
  remains in generated `~/.zshrc`.
- Activated desktop once at `24/06/2026 14:34 -03`, which added
  `THROTTLE_ACTIVE_CRED_PATH=/home/notroot/.claude/.credentials.json` but still
  left the service on the old PR #55 proxy package.
- Merged `phsb5321/NixOS#1031` at `24/06/2026 14:42 -03`: bumped
  `pkgs/anthropic-throttle-proxy` from PR #55 (`2ad7fc8`) to PR #59
  (`741d1be9d0baa54c536a16304e0eaaec50412d98`).
- Activated desktop again at `24/06/2026 14:44 -03`.
- Final systemd readback showed the live service restarted at
  `24/06/2026 14:44:57 -03` with:
  - `ExecStart=/nix/store/296fif3ij846sv741vf5h8ji0pgmxgkg-anthropic-throttle-proxy-0.1.0/bin/anthropic-throttle-proxy`
  - `THROTTLE_ACTIVE_CRED_PATH=/home/notroot/.claude/.credentials.json`
  - `ActiveState=active`, `SubState=running`
- Final `/__throttle/health` showed `inflight=0`, `queued=0`,
  `upstream_egress_ok=true`, `central_status=up`, and a clean in-memory
  `bearers={}` state after the service restart.
- Verified the live store path contains `_active_account_bearer`,
  `ACTIVE_CRED_PATH`, and `credential-nudge`.

### Extra discrepancy found

The 23/06 entry says `fable-tag` was repaired to require an explicit model.
That was not durable. On 24/06, live `~/.zshrc` and the NixOS source still had
an implicit malformed default:

    local dir="${1:-$PWD}" model="${2:-claude-fable-5[1m]}"

This was NOT the current rate-limit cause: the repo-local
`.claude/settings.local.json` was absent, and `~/.claude/settings.json` plus
`~/.claude/settings.json.tmp` had no `model` key. This discrepancy is now
closed by NixOS PR #1030 and the
`24/06/2026 14:34 -03` desktop activation; generated `~/.zshrc` contains the
`usage: fable-tag <dir> <model>` guard and no `claude-fable-5[1m` match.

---

## 23/06/2026 - desktop Claude Code tabs all rate-limited

### What was wrong

Pedro reported that no Claude Code tab on desktop was working and asked whether
the previous session `c430b7c3-a6ac-4293-bf40-590a663a3ebe` had broken
anything.

Live proxy state was healthy: `/__throttle/health` returned
`inflight=0`, `queued=0`, `queue_mode=fair`, `central_status=up`, and
`upstream_egress_ok=true`; `/` returned `anthropic-throttle-proxy`. The
service was also active under systemd with no transient override drop-in.

There were two independent local problems:

1. `~/.zshrc` had a `fable-tag` helper with an implicit malformed default model
   (`claude-fable-5[1m`, later accidentally reduced to `claude-fable-5]` during
   cleanup). This could write bad `.claude/settings.local.json` files and force
   Claude Code away from its default model selection.
2. The repo root had an untracked `.claude/settings.local.json` containing only
   `"model": "claude-fable-5[1m]"`, so Claude Code launched from this repo read
   a bad project-local model override.

The previous session was also manipulating Claude account profiles. Its task
list included logging `~/.claude-b` into `pedrobalbino@pm.me` and a pending
task to restore `~/.claude` to `pedrobalbino@proton.me`. At repair time,
`~/.claude/.claude.json` already identified A as `pedrobalbino@proton.me`.

### What was repaired

- Changed `fable-tag` in `~/.zshrc` so it has no implicit model default. It now
  requires an explicit model argument before writing any `model` key.
- Removed the repo-local untracked `.claude/settings.local.json`, letting
  Claude Code use its default model from this repo.
- Removed the stale malformed `model` key from `~/.claude/settings.json.tmp`.
- Terminated 40 stale `.claude-wrapped` processes with `TERM` so new tabs load
  the repaired settings and current credential files. No `KILL` was needed.

### Remaining hard limit

The proxy still reported real upstream quota pressure after cleanup. Three
bearer hashes were held behind `Retry-After` until the 7-day reset at
`26/06/2026 07:00 -03`; one had `util_7d=1.0`. The current A/B credential
hashes did not match those blocked hashes, so fresh Claude tabs should not
inherit the old in-memory exhausted tokens. If new tabs still fail, capture a
fresh `/__throttle/health` and map the active credential hash before changing
proxy behavior.

### Verified state after cleanup

- No `.claude-wrapped` processes remained.
- `~/.claude/settings.json` and `~/.claude/settings.json.tmp` both returned
  `has("model") == false`.
- The repo-local `.claude/settings.local.json` no longer existed.
- `~/.claude/.claude.json` reported `pedrobalbino@proton.me`.
- `/__throttle/health` still returned `inflight=0`, `queued=0`,
  `central_status=up`, and `upstream_egress_ok=true`.

---

## 06/06/2026 — central nginx upstream stale after Dokku-host reboot

### What was wrong

Pedro's prior Claude Code session crashed with:

    API Error: The socket connection was closed unexpectedly. This is often a
    network or firewall issue. ...
    500 ... anthropic-throttle.home301server.com.br

Local proxy stayed healthy. The failure was central-only, between Dokku's
nginx vhost and the `anthropic-throttle.web.1` container.

**Causality status: ◐ partially verified, NOT proven.**
`dokku proxy:build-config anthropic-throttle` repaired the central tier,
but the pre-recovery `/home/dokku/anthropic-throttle/nginx.conf` was
never captured. The stale-upstream-IP theory below is the *most likely
mechanism*, not a proven root cause. The missing falsifier is the
pre-recovery upstream IP (or nginx error-log lines showing connection
attempts against an old container IP). See "Required wording change"
note at the bottom of this section.

Most likely mechanism, in five steps. Each line marks whether the
evidence was captured pre-recovery (P), post-recovery (POST), or is
inference (I) not measured here:

1. (P) Pedro ran `sudo apt-get upgrade -y` on the Dokku host at 19:11:29
   on 2026-06-06 (`/var/log/apt/history.log`). The upgrade bumped
   `docker-ce` / `docker-ce-cli` / `docker-ce-rootless-extras`
   29.5.2 → 29.5.3, `dokku` 0.38.10 → 0.38.17, plus `cloud-init`,
   `apparmor`, `firmware-sof-signed`, `trivy`. (P) `dockerd` restarted
   at 19:11:48 (host journal). (I) "First container-IP-shuffle event"
   is inferred from Docker IPAM behavior — not measured here, because
   the pre-event container IP was not captured.

2. (P) At 19:17:49 Pedro ran `sudo systemctl reboot` (`sudo[2284478]:
   notroot : COMMAND=/usr/bin/systemctl reboot`, followed by
   `systemd-logind: System is rebooting`). Orderly shutdown, not a
   crash. The pre-staged kernel (5.15 → 5.15.124, installed Jun 3 06:00
   by unattended-upgrades) finally activated. (I) "Second IP-shuffle
   event" — again inferred, not measured.

3. (POST) `anthropic-throttle.web.1` came up on bridge IP `10.0.0.24`
   (`dokku ps:report` after recovery). Docker's default IPAM is
   sequential, not stable across daemon restarts or host reboots, so
   the IP *may* have been different before either event — but the
   pre-event IP was never captured, so this remains hypothesis.

4. (I, unproven) Dokku does NOT auto-regenerate per-app
   `/home/dokku/<app>/nginx.conf` on host reboot, on dockerd restart,
   or on container respawn. The hypothesis is that the vhost kept
   serving a stale `upstream` directive against an unreachable old IP.
   This is the unproven step: the pre-recovery vhost was not preserved
   before running `proxy:build-config`, which destroyed the forensic
   artifact.

5. (I) nginx returned 502 / `connection closed by upstream` to the
   local proxy. Local proxy's central forward failed; PR #23
   (25/05/2026 fix, merged as `78c2c43d`) promoted local admission to
   `fair` and fell back to direct upstream. Pedro saw a user-visible
   `500 ... anthropic-throttle.home301server.com.br`, which means
   *some* failures escaped the queue + retry path. Whether that is
   expected (e.g. the central response had already started streaming
   when nginx returned 502, so retry was unsafe) or a remaining
   fallback bug is an open question — see Follow-up #5.

Plausible alternatives that are NOT ruled out and would also explain
the symptom:

- The Dokku 0.38.10 → 0.38.17 upgrade itself changed proxy-config
  behavior, failed a postinst hook, or left nginx serving an older
  config until `proxy:build-config` repaired it. The doc does not
  prove this is *not* what happened.
- `nginx -t` could have failed silently after the upgrade. The cert
  mismatch on the same vhost (Follow-up #1) is independent evidence
  that nginx-side state on this host is already in a confused state,
  which makes silent reload failure not implausible.
- `dokku-watchdog` or app-level healthcheck flap could have
  temporarily marked the container unhealthy and routed nginx away
  from it. We did not pull watchdog / Docker-event / healthcheck logs
  before recovery.
- This may not be app-specific. We did not enumerate other Dokku
  apps' `/home/dokku/<app>/nginx.conf` vs live `dokku ps:report` IPs,
  so the scope ("only this app drifted" vs "many drifted") is
  unverified.

### The first hypothesis was wrong

Initial framing: "unattended-upgrades nightly run rebooted the host
because a kernel upgrade landed."

Falsified by:

- `last reboot | head -3` → `Sat Jun 6 19:19  still running`, prev boot
  `Jun 4 04:01 → Jun 6 19:17`. Single recent boot, matched the
  failure window.
- `last shutdown | head -3` → `Sat Jun 6 19:17 - 19:19 (00:01)`. Orderly
  1-minute shutdown.
- Journal at 19:17:49 → `sudo[2284478]: notroot :
  COMMAND=/usr/bin/systemctl reboot`. Pedro triggered it manually.
- `/var/log/unattended-upgrades/unattended-upgrades.log` had no 19:1x
  entries on 2026-06-06; last cycles were 06:20 and 09:35. The kernel
  was pre-installed at 06:00 on Jun 3 but only activated by Pedro's
  manual reboot.

Actual trigger: Pedro's interactive `apt-get upgrade -y` (which cycled
dockerd as a side effect of the docker-ce 29.5.2 → 29.5.3 bump),
followed by `systemctl reboot`. Whether *those host events* caused
container IP drift, and whether IP drift caused the bad vhost, is the
unproven step (see "Causality status" above).

### The fix that shipped

Recovery was a single Dokku command on the host:

    dokku proxy:build-config anthropic-throttle

This regenerated `/home/dokku/anthropic-throttle/nginx.conf` with
`upstream anthropic-throttle-8765 { server 10.0.0.24:8765; }`. The
command's own output reported nginx reload success, but no separate
`nginx -t` / `systemctl reload nginx` / `journalctl -u nginx` /
`nginx -T` was captured to independently prove workers picked up the
new config — see Mistake #6.

Verified after recovery (all POST-recovery; the matching pre-recovery
captures do not exist):

- `curl http://anthropic-throttle.home301server.com.br/__throttle/health`
  from the Dokku host (NOT from the local-proxy host through DNS) →
  `queue_mode=fair`, `served` counter climbing, no 502.
- Local proxy `/__throttle/health` (loopback `127.0.0.1:8765`) →
  `central_status=up`, `via=null` (central path active), `served`
  resuming. This proves the local proxy's *own* central probe
  recovered; it does not independently re-test the same DNS + nginx
  vhost + container path the failing requests took. See Mistake #2.
- `/home/dokku/anthropic-throttle/nginx.conf` mtime updated to 19:43,
  post-recovery upstream IP matched `dokku ps:report` container IP.
  The pre-recovery upstream IP and mtime were not captured, so we
  cannot show the file actually changed in a way that would explain
  the symptom.

Container at recovery (POST):

- CID `fa93c1b363c`, IP `10.0.0.24`, FinishedAt 19:19:47 → StartedAt
  19:19:51, RestartCount=0, ExitCode=0, OOMKilled=false. Reboot
  recovery, not a deliberate dokku op.

No code or Nix pin changed. This was a host-side data-plane recovery,
not a proxy-software bug.

### Open follow-ups

These are separate from the inline incident work above; tracked here
so the next session does not re-discover them. Numbered for
cross-reference from Mistakes and the workflow section.

1. **TLS regression on the external HTTPS endpoint — incident-relevant,
   not cosmetic.** `curl -v
   https://anthropic-throttle.home301server.com.br/__throttle/health`
   serves the `ai-docs.home301server.com.br` certificate — SAN
   mismatch. Local proxy uses `THROTTLE_CENTRAL_URL=http://...` so the
   workflow is unaffected, but the public HTTPS path is broken AND it
   means the nginx server-name → cert binding for this vhost is
   already in a confused state. That makes the silent-stale-config /
   silent-reload-failure alternative (see "What was wrong" plausible
   alternatives) more credible, not less. Investigate `dokku
   letsencrypt:list` together with this incident's nginx state, not
   as a separate cosmetic bug.

2. **Systemic guard against this exact failure mode.** The host
   already runs `/usr/local/bin/authelia-upstream-sync.sh` every
   minute via cron to resync that app's upstream IP after container
   drift. The same pattern generalizes: a small cron that diffs
   `dokku ps:report <app>` container IP against the upstream in
   `/home/dokku/<app>/nginx.conf` and runs `dokku proxy:build-config
   <app>` on mismatch — for *every* Dokku app, not just this one.
   Alternative: pin container IPs via Docker IPAM. Either prevents
   the next reboot or `apt upgrade docker-ce` from silently breaking
   any app on this host.

3. **Post-upgrade / post-reboot Dokku app proxy consistency check.**
   Add a one-shot script that runs after every `apt upgrade` and on
   every boot: enumerate `/home/dokku/*/nginx.conf`, compare each
   `upstream` IP to the live `dokku ps:report` container IP, and
   either alert or auto-`proxy:build-config` on mismatch. Closes the
   same gap as #2 but at the boundary, not on a polling cron.

4. **Local proxy log/metric distinguishing central failure modes.**
   The local proxy currently treats any central forward failure as
   one bucket. Distinguish: HTTP 502 from nginx, connection refused
   (no nginx), socket close mid-stream, DNS failure, timeout. The
   user-visible 500 in this incident does not tell us which one
   Pedro hit, which makes it impossible to decide whether the
   fallback behaved as designed. Add separate counters
   `central_failure_total{kind="..."}`.

5. **Regression test: does central HTTP 502 trigger direct fallback?**
   Open question from "What was wrong" #5 / Mistake #4. Add a test
   that stands up a fake central returning 502, runs a request
   through the local proxy in central-configured mode, and asserts
   either (a) the request succeeded via direct fallback, or (b) the
   request failed with a documented status. Either way the answer
   becomes contract, not folklore.

6. **Alert for central returning 502 from nginx.** Prometheus rule
   on the local proxy or the central tier (whichever sees nginx 502
   on the central forward) so the next occurrence pages instead of
   surfacing as a session crash. Pair with a runbook entry pointing
   at this handoff.

7. **Audit ALL Dokku apps for stale upstream risk now, not after the
   next incident.** Run the diff in #3 once today across the host;
   find any other apps whose vhost upstream IP doesn't match the
   live container, and rebuild them. The reboot at 19:17 may have
   left more apps in this state — we did not check. Plain HTTP
   smoke tests of externally important apps would be a faster
   first pass than enumerating files.

8. **Enable `dokku events:list` event logging.** Currently disabled
   on this host, so the Dokku-side audit trail beyond the journal
   is gone. Enable it as part of systemic hardening; this incident
   would have been easier to triage with a Dokku-side event timeline.

### Mistakes made during the session

Do not repeat these. Numbered for cross-reference.

1. **The biggest miss: failed to preserve pre-recovery evidence
   before running `dokku proxy:build-config`.** The pre-recovery
   `/home/dokku/anthropic-throttle/nginx.conf` (with the suspected
   stale upstream IP) was never copied or printed. The fix command
   destroyed the most important forensic artifact in this incident.
   Also missed before recovery: `nginx -T | grep -A3
   anthropic-throttle`, `/var/log/nginx/error.log` lines for the
   failing hostname, `docker events --since '30m ago'`, `dokku logs
   anthropic-throttle`, full `docker inspect anthropic-throttle.web.1`.
   After this, root cause could only be inferred, not proven.

2. **Did not verify central health from the failing path.**
   Re-checked health by curling
   `http://anthropic-throttle.home301server.com.br/__throttle/health`
   from the Dokku host itself, and by hitting `127.0.0.1:8765` on the
   local proxy. Neither retraces the actual failing path: from the
   local-proxy host, through the configured `THROTTLE_CENTRAL_URL`,
   through DNS, through nginx vhost, to the Dokku container. That
   end-to-end check was skipped.

3. **Did not check blast radius across other Dokku apps.** If host
   reboot or `apt upgrade docker-ce` causes upstream-IP drift, every
   per-app vhost on this host is at risk. The session never
   enumerated `/home/dokku/*/nginx.conf` upstreams against live
   `dokku ps:report` IPs. Other apps may still be drifting silently.

4. **Did not rule out alternative causes.** The four candidates
   below were left uneliminated, any of which would have produced
   the same symptom:
   - The Dokku 0.38.10 → 0.38.17 upgrade itself (config-handler
     behavior change, failed postinst hook, nginx-config-test
     failure after the upgrade).
   - `dokku-watchdog` or app-level healthcheck flap routing nginx
     away from a temporarily-unhealthy container.
   - A stale `nginx -t` failure that prevented reload and left
     workers on an old config, made more plausible by the cert
     mismatch on the same vhost (Follow-up #1).
   - A central-side throttle-proxy crash that recovered before
     evidence was captured.

   Pulling `dokku logs anthropic-throttle`, watchdog logs, Docker
   events, and `journalctl -u nginx` *before* recovery would have
   ruled these in or out.

5. **Did not run `dokku events:list` early.** It would have been
   the fastest Dokku-side audit signal. The fact that it returns
   "Events logger disabled" on this host is itself a finding —
   that should have been caught in this session, not left as a
   passive follow-up.

6. **Did not independently verify nginx reload after
   `proxy:build-config`.** The fix command's own stdout reported
   reload success; no separate `nginx -t`, `systemctl status nginx`,
   `journalctl -u nginx --since`, or `nginx -T` was run to prove
   workers picked up the new config. Given the cert mismatch on the
   same vhost (Follow-up #1), nginx state on this host is already
   suspect and silent reload failure is not impossible.

7. **Treated `apt upgrade` and `unattended-upgrades` as synonyms.**
   They are different operators (manual interactive vs nightly
   background), with different evidence trails. Always check
   `/var/log/apt/history.log` AND
   `/var/log/unattended-upgrades/unattended-upgrades.log`, not just
   one.

8. **First search for stale nginx config looked under
   `/etc/nginx/conf.d/`.** Since Dokku 0.30 the per-app vhost lives
   at `/home/dokku/<app>/nginx.conf`, with overrides under
   `/home/dokku/<app>/nginx.conf.d/`. Verify `dokku version` (0.38.17
   here) before locating files.

9. **Initial hypothesis named "kernel auto-reboot" without first
   running `last reboot` and `last shutdown`.** The trigger evidence
   was one journal line away. Capture audit trails before forming
   hypotheses.

10. **Did not initially separate "container respawned" (`FinishedAt
    19:19:47 → StartedAt 19:19:51`) from "host rebooted" (boot ID
    flip at 19:17:56 → 19:19:42).** The four-second container gap
    looked like a deliberate `dokku ps:restart`; it was actually
    Docker-daemon recovery during the reboot. `last reboot` plus
    boot ID inspection disambiguates.

### How Claude should have solved it

The correct workflow when central is unhealthy and local is fine:

1. **Capture host-side audit trail first, before forming hypotheses:**
   - `last reboot | head -3`
   - `last shutdown | head -3`
   - `journalctl --since '2 hours ago' | grep -E 'sudo|systemd-logind: System is'`
   - `/var/log/apt/history.log` last block (manual upgrades)
   - `/var/log/unattended-upgrades/unattended-upgrades.log` last cycle
   - `dokku version` (locates per-app nginx.conf path)
   - `dokku events:list` (and surface "logger disabled" as a
     finding, not a passive follow-up)

2. **State hypothesis in one sentence with an explicit falsifier.**
   Example: "central nginx upstream is stale because the container IP
   changed after a host event; falsifier: if `dokku ps:report` IP
   matches the `upstream` line in `/home/dokku/<app>/nginx.conf`, the
   IP-drift theory is wrong."

3. **Preserve pre-fix evidence — this is the step the 06/06/2026
   session skipped.** Before any recovery command, copy or print:
   - `/home/dokku/<app>/nginx.conf` (the suspected stale file —
     `cp` it aside, do NOT just `cat` it once)
   - `nginx -T 2>/dev/null | grep -A 3 -E "<app>|<hostname>"`
   - `/var/log/nginx/error.log` lines for the failing hostname
     (`grep <hostname> /var/log/nginx/error.log | tail -200`)
   - `journalctl -u nginx --since '2 hours ago'`
   - `docker events --since '30m ago' --until 'now'` (post-hoc
     replay; otherwise capture live next time)
   - `dokku logs <app> --tail`
   - `docker inspect <app>.web.1` full output
   - `dokku ps:report <app>` (current container IP)
   - For multi-network apps, every IP from `docker inspect`'s
     `NetworkSettings.Networks` map, not just the bridge IP

4. **Prove or falsify the upstream-IP path against current state:**
   - Diff captured `dokku ps:report` IP against `grep -A1 upstream
     /home/dokku/<app>/nginx.conf`.
   - Mismatch → confirms IP drift, the most likely mechanism.
   - Match → IP drift falsified; reach for the other candidates
     (Mistake #4 alternatives: Dokku upgrade behavior, watchdog
     flap, failed nginx reload, cert/server-name binding bug,
     central-proxy crash).

5. **Apply minimal reversible recovery:**
   - `dokku proxy:build-config <app>`

6. **Independently verify nginx reload, NOT just the Dokku
   command's stdout:**
   - `nginx -t` (must print `syntax is ok` / `test is successful`)
   - `journalctl -u nginx --since <timestamp>` (look for reload
     line + zero errors)
   - `nginx -T 2>/dev/null | grep -A 3 <app>` confirms live config
     matches the new file
   - File mtime alone (as in this incident) is NOT sufficient —
     workers may not have picked it up.

7. **Re-verify the failing path end-to-end, not loopback:**
   - `curl -v $THROTTLE_CENTRAL_URL/__throttle/health` from the
     local-proxy host, through DNS + nginx vhost + container.
     Loopback `127.0.0.1:8765` and Dokku-host-local curls do NOT
     retrace the failing path.
   - Local proxy `central_status=up` and `via=null` are necessary
     but not sufficient.

8. **Audit blast radius before declaring done:**
   - Enumerate `/home/dokku/*/nginx.conf` upstream IPs vs `dokku
     ps:report` per app on the same host. If multiple apps drifted,
     this is a host-wide event, not an app-specific one.

9. **Document this incident, run Codex adversarial review per the
   section below, address findings, ship the doc PR, then file
   the open follow-ups as separate issues.**

### Required wording change (carried over from Codex review)

Per Codex's adversarial review of an earlier draft of this section:
the root-cause language was overclaiming. The honest framing —
preserved here so a future session does not regress it — is:

> Most likely mechanism: host upgrade/reboot caused container or
> proxy state drift, and `dokku proxy:build-config anthropic-throttle`
> repaired nginx-to-container routing. We did not preserve the
> pre-recovery nginx upstream, so stale-IP causality is not proven.
> The missing falsifier is the old upstream IP or nginx error logs
> showing connection attempts to an old container IP.

Mark every future incident's evidence with (P) / (POST) / (I) so the
proven-vs-inferred boundary stays visible.

---

## 25/05/2026 — local fallback bypassing local queue

### What was wrong

Pedro reported Claude Code requests sitting at "typed but no spinner" and
server-side rate limiting while the local proxy was configured as
`THROTTLE_QUEUE_MODE=off`.

The live system had two separate issues:

1. Central throttle was doing real queueing.
   `http://anthropic-throttle.home301server.com.br/__throttle/health` showed
   `queue_mode=fair`, active queued work, and high 7-day utilization. Some delay
   was expected because the central tier intentionally holds requests before
   upstream response start.

2. Local fallback was unsafe when central flapped.
   The desktop local tier was configured with `THROTTLE_QUEUE_MODE=off` because
   central owns normal fleet-wide queueing. When central went down or a central
   forward failed, the local tier retried direct upstream while still in
   pass-through mode. That meant a central flap could become an unqueued direct
   firehose against Anthropic.

The fix that shipped:

- `yolo-labz/anthropic-throttle-proxy#23`
  - merged as `78c2c43d48228ddd09e543a2219019209ffbd17e`
  - promotes local admission to `fair` when central is configured but routing is
    direct fallback
  - serializes emergency direct retries after a central forward failure
  - keeps promoted fair limiters from being downgraded back to `off`
- `phsb5321/NixOS#703`
  - merged as `6810250dcef68637d047ac6544911b4401deac30`
  - bumps the Nix package pin to the fixed upstream commit
- Host activation completed with:
  - `/nix/store/055538y8i9r96lycpq5mx2pc6yk6mhnv-nixos-system-desktop-26.05.20260523.3d8f0f3`
  - active user unit points at `/nix/store/ffl8ngpi6y4f49vps94r6w7w89nfsa59-anthropic-throttle-proxy-0.1.0`

### Mistakes made during the session

Do not repeat these.

- The first response treated sandbox restrictions as a stopping point too early.
  The right move was to keep looking for a route: GitHub API/`gh`, `/tmp`
  worktree/clone, Nix build with a writable cache, and host activation from a
  clean checkout.
- A temporary workspace proxy was started while systemd was also trying to own
  `127.0.0.1:8765`. That created the bind collision that made the fixed systemd
  unit restart-loop. If you start an emergency process, record its PID and kill
  it before handing control back to systemd.
- The first NixOS PR upload accidentally emptied
  `pkgs/anthropic-throttle-proxy/default.nix` by using Perl in-place editing
  with shell redirection. Always parse/check generated Nix files before upload.
- Local dirty main was left behind from the emergency code edit. Future doc or
  follow-up work must use a dedicated worktree first.
- Early checks looked at the symptom, but the important proof came from
  combining health JSON, journal lines, and the exact fallback path in code.
  Similar-looking rate-limit incidents are not enough evidence.

### How Claude should have solved it

The correct incident workflow is:

1. Capture live state first:
   - `curl http://127.0.0.1:8765/__throttle/health | jq`
   - central health at `THROTTLE_CENTRAL_URL`
   - `journalctl --user -u anthropic-throttle-proxy -n 200 --no-pager`
   - current `ANTHROPIC_BASE_URL`, service `ExecStart`, and Nix store path
2. State the hypothesis in one sentence.
   Example: "Local central fallback is bypassing the local queue in `off` mode,
   so central flaps create direct unqueued Anthropic bursts."
3. Prove or falsify that hypothesis against the exact code path:
   - `pick_target()`
   - `_get_bearer_limiter()`
   - `_retry_direct_once()`
   - queue mode transitions in `FairBearerLimiter`
4. Apply the smallest live mitigation that is reversible:
   - `/ui/config` or direct POST to set `min_dispatch_gap_ms`
   - verify it persisted in
     `~/.local/state/anthropic-throttle-proxy/overrides.json`
5. Patch the repo in a feature worktree and test:
   - targeted forwarding tests
   - full pytest
   - ruff
6. Publish and merge the upstream proxy fix.
7. Bump the NixOS package pin and fixed-output hash.
8. Wait for NixOS CI, including host eval.
9. Activate the host from a clean checkout, not the dirty repo checkout.
10. Verify the actual runtime:
    - user unit `ExecStart` points to the fixed store path
    - `curl http://127.0.0.1:8765/__throttle/health` responds
    - journal shows the fixed store path and no bind failure

---

## Required adversarial review (applies to all incidents)

Before merging a throttle behavior change, ask Codex for adversarial review.
The review prompt must include:

- the user-visible symptom
- the one-sentence hypothesis
- live health/journal evidence, marked (P) pre-recovery, (POST)
  post-recovery, or (I) inference
- the exact code diff (or recovery action, for host-side incidents)
- test results
- the deployment/pin/activation plan
- for host-side incidents: pre-recovery captures of the suspected
  stale config (e.g. `/home/dokku/<app>/nginx.conf`), nginx error
  logs for the failing hostname, `docker events`, `dokku logs`,
  and the relevant `journalctl -u nginx` window

Codex must be asked to look specifically for:

- unproven causality, especially "correlation dressed up as root cause"
- fallback paths that still bypass local queueing
- limiter mode transitions that can strand queued futures
- central/local behavior mismatches
- Nix pin or fixed-output hash mistakes
- host activation gaps where merged code is not actually running
- for host-side incidents: container-IP drift, stale per-app nginx
  vhost, mismatch between `dokku ps:report` and
  `/home/dokku/<app>/nginx.conf`, silent `nginx -t` / reload
  failures, and blast radius across other apps on the same host
- whether pre-recovery forensic artifacts were preserved before the
  fix command was run

Do not merge or declare the incident solved until the adversarial findings are
addressed or explicitly documented with evidence.

---

## 29/06/2026 — short Retry-After fast-fail incident

### Symptom

Claude tabs surfaced:

```text
API Error: Server is temporarily limiting requests (not your usage limit) · upstream retry-after window is active; failing fast instead of holding the local gateway request
```

### Verified cause

Anthropic returned short `Retry-After: 30` throttles for the active OAuth
bearer while the proxy default held only `15s`. The proxy therefore fast-failed
requests that should have been held and retried. Official docs say earlier
retries before `retry-after` expires will fail, and Claude Code's own error
guide classifies "Server is temporarily limiting requests" as a short-lived
throttle unrelated to plan quota.

Live mitigation applied at 29/06/2026 17:32 -03:

```bash
POST /ui/config key=max_hold_retry_after_s value=60
POST /ui/config key=max_concurrent value=5
POST /ui/config key=central_local_max_concurrent value=5
POST /ui/config key=aimd_initial_concurrent value=3
POST /ui/config key=aimd_backoff_s value=30
POST /ui/config key=aimd_ramp_after value=6
```

This fixed the visible fast-fail wording, but did not fully solve rate
pushback. Central Dokku logs at `29/06/2026 17:37 -03` showed `cbabb12c`
dispatching 4-5 concurrent Opus requests and receiving upstream `429` twice:

```text
rate-pushback-retry bid=cbabb12c status=429 retry=1/2 pause=30.0 retry_after=0.0
aimd-shrink bid=cbabb12c status=429 max_concurrent=3 ...
rate-pushback-retry bid=cbabb12c status=429 retry=1/2 pause=30.0 retry_after=0.0
aimd-shrink bid=cbabb12c status=429 max_concurrent=1 ...
```

Second mitigation applied at `29/06/2026 17:40 -03`:

```bash
POST /ui/config key=max_concurrent value=3
POST /ui/config key=central_local_max_concurrent value=3
```

Post-second-mitigation health: local `cbabb12c` `lim_live=3`, `lim_hard=3`;
central `cbabb12c` `lim_live=2`, `lim_hard=3`; both status `allowed`, 5h
utilization `33%`, 7d utilization `26%`. The falsifier is a fresh central
`429` while central live concurrency is `<=3`.

### Durable fix

Keep `THROTTLE_MAX_HOLD_RETRY_AFTER_S` default at `60`, not `15`, so common
short 30s upstream throttles are absorbed. Multi-hour account-window
`Retry-After` values must still fast-fail or trigger the stale-credential 401
nudge.

Do not raise `CLAUDE_API_THROTTLE_MAX` above `3` for the active Claude Code
Opus-heavy fleet unless fresh central evidence shows clean operation above
that. The SOTA follow-up is not another static number: implement adaptive
send-rate / weighted admission keyed by model and request size, using
`Retry-After`, 429 density, queue delay, and client-disconnects as feedback.

### Follow-up: disconnected queued clients

At `29/06/2026 18:27:59 -03`, desktop restarted the local proxy into the
new package (`served` reset, pid changed from `573099` to `2633722`). Socket
activation kept the port mostly available, but several in-flight upload bodies
reset during `request.read()`, then the restarted tabs created an Opus-heavy
burst. Local health showed `max=3`, active bearer `cbabb12c` `status=allowed`,
and no fresh upstream retries; central health also had `upstream_retries=0`.
The visible API errors were therefore not new Anthropic 429s. They were client
disconnects while queued/streaming:

```text
client-disconnect ... bid=cbabb12c ... model=claude-opus-4-8 ... no_upstream_retry=true
ConnectionResetError: [Errno 104] Connection reset by peer
```

Durable fix in PR #68: treat body-read resets as 499 client disconnects, and
check the aiohttp client transport after fair-queue admission and again after
any Retry-After wait. If the Claude tab has already gone away, release the
slot and do not forward to central/upstream.

## 07/07/2026 — phantom-401 storm: bounded queue wait (PR #83)

Fleet-wide `API Error: 401 InvalidHTTPResponse fetching /v1/messages?beta=true`
was NOT a real 401. With only 2/5 bearers usable (A expired without a
refreshToken, C empty), fair-queue waits reached 60-80 s. Claude Code aborts a
silent request at ~60 s and closes its socket; the proxy's late write then hit
a closing transport:

```text
client-disconnect where=first path=/v1/messages ... via=central
elapsed_ms=75123 exc='ClientConnectionResetError: Cannot write to closing
transport' no_upstream_retry=true
```

The truncated HTTP surfaced client-side as Node fetch `InvalidHTTPResponse`,
which claude-code misclassifies as a 401/login failure (matches
routatic/proxy#62). No knob bounded queue time — `max_hold_retry_after_s`
gates only Retry-After holds.

Durable fix in PR #83 (merged `c8431f8`; central deployed same day; desktop
pin bumped in NixOS #1175):

- `THROTTLE_QUEUE_MAX_WAIT_S` (default 30 s, hot-tunable, 0=off) bounds the
  fair-queue wait. Exceeding it returns a clean
  `503 + Retry-After: 5 + x-anthropic-throttle-queue-timeout: 1` while the
  client transport is still alive, so the SDK retries transparently.
- The bound is end-to-end: each tier forwards the remaining budget via
  `x-anthropic-throttle-wait-budget-ms` and the next tier takes
  `min(own knob, inherited)`; every central attempt restamps the remaining
  budget so pushback-retry sleeps cannot re-grant central a full window.
  Client-supplied copies (any case) are stripped and re-stamped canonically.
- A local tier relays the stamped 503 verbatim — exempt from pushback-retry
  and AIMD shrink (central queue depth is admission backpressure, not the
  bearer's upstream pushback). The stamp is only trusted from marker-bearing
  proxy responses; a raw upstream 503 carrying it is stripped and still
  shrinks.

Codex adversarial review ran three rounds and every finding was real:
round 1 caught the local+central stacking BLOCKER and the spoofable-header
MAJOR; round 2 caught the stale-budget-on-retry and mixed-case-duplicate
BLOCKERs; round 3 approved. Single-pass review would have shipped a >60 s
silence hole.

Capacity itself is the other half of the incident: the fleet needs account A
re-logged (Pedro-gated OAuth) — the proxy fix makes saturation *clean*, not
free. Residual: `max_hold_retry_after_s=60` can still hold one Retry-After
near the client's patience window; lower it via `/ui/config` if disconnect
logs persist at high queue depth.
