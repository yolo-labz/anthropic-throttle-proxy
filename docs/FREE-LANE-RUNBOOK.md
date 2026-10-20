# Free-lane runbook — OpenCode zen/go throttle gates

**Scope**: the two OpenCode throttle gates that serve the fleet's `*-free`
models to pi seats — the **zen gate** (`:8774`) and the **go gate** (`:8770`)
— plus their three sibling gates (`zai :8766`, `mimo :8773`) where the same
knobs apply. Mission: run the whole free catalog at **maximum parallel width
without ever triggering upstream rate limiting** — *queue, never cross the
line*.

**Ground truth**: live incident of 19/10/2026 ~18:41–19:06 BRT on the go
gate, replayed in §5 with journal receipts. All timestamps are
`America/Recife` (`-03:00`), which is what
`journalctl -o short-iso` emits on this host.

**Status of this document**: written 19/10/2026 ~19:30 BRT from code reads
(`src/anthropic_throttle_proxy/`), systemd unit/drop-in files, and the live
journals of the four gate services. Every operational claim below carries a
receipt (`file:line`, `stat`, or verbatim log line). Items that could **not**
be verified are listed in §8 — do not treat them as facts.

---

## 1. The never-cross-the-line invariant

The upstream (`opencode.ai/inference` zen / `opencode.ai/inference/go`) rate
limits free-model traffic. Once a 429 lands, the shared credential is pinned
at a shrunk concurrency floor and every other request starves behind the one
long generation that holds the last slot (§5). Therefore:

1. **A 429 on a free gate is a crossed line, not a nuisance.** The gate's job
   is to queue *below* the line; AIMD shrink is the reaction, not the plan.
2. **Width is earned, never hand-set.** The AIMD ceiling starts at
   `THROTTLE_AIMD_INITIAL_CONCURRENT` (12 on both OpenCode gates) and grows
   additively — `THROTTLE_AIMD_RAMP_AFTER` successes per step
   (default 10, `config.py:233`) past the `THROTTLE_AIMD_BACKOFF_S` cooldown
   (default 30 s, `config.py:155`). Never raise a live ceiling by hand to
   "recover throughput"; restart + natural growth is the sanctioned path.
3. **The floor never collapses below the configured minimum.**
   `AIMD_MIN = max(1, …)` is clamped in code (`config.py:151`) so traffic can
   never fully block; the go gate and mimo carry a raised floor of **2**
   (§2.2). An operator setting `THROTTLE_AIMD_MIN=0` is defeated by the clamp
   (regression-guarded in `tests/test_pacing.py:248-252`).
4. **Queue, don't cross.** `THROTTLE_QUEUE_MODE=fair` parks excess requests
   inside the budget (`THROTTLE_QUEUE_MAX_WAIT_S=180`) instead of forwarding
   them upstream; queue-depth admission refuses a request whose estimated
   wait already exceeds its budget *before* parking it, so the gate never
   manufactures a doomed wait (§4.5).
5. **The client ladder must hold** — gate queue wait (180 s) < hold ceiling
   (240 s) < pi HTTP idle timeout (300 s) — so a refusal always reaches the
   client as a clean, retryable `503` with a `Retry-After`, never as a
   truncated transport (§3).

---

## 2. Topology and the live operating contract

### 2.1 Gates

| Gate | systemd user unit | Port | Upstream | Binary |
|---|---|---|---|---|
| **zen** (free catalog) | `opencode-zen-throttle-proxy.service` | `127.0.0.1:8774` | `https://opencode.ai/inference` | `throttler-gateway` |
| **go** (free catalog, subscription) | `opencode-go-throttle-proxy.service` | `127.0.0.1:8770` | `https://opencode.ai/inference/go` | `throttler-gateway` |
| zai (GLM Coding Plan) | `zai-throttle-proxy.service` | `127.0.0.1:8766` | `https://api.z.ai` | `anthropic-throttle-proxy` |
| mimo (Token Plan) | `mimo-throttle-proxy.service` | `127.0.0.1:8773` | `https://token-plan-sgp.xiaomimimo.com` | `anthropic-throttle-proxy` |

Receipt: `systemctl --user cat <unit>`; go banner
`listening on 127.0.0.1:8770 … upstream=https://opencode.ai/inference/go
central=(direct)` (19/10 19:10:03, pid 2736866); zen banner
`listening on 127.0.0.1:8774 … upstream=https://opencode.ai/inference`
(19/10 11:31:01, pid 2457800). pi routes `opencode` → `:8774`,
`opencode-go` → `:8770` (NixOS PRs #2878/#2879; vault note
`1. Projects/Herdr Fleet Coordination/2026-10-17 — Pi model providers —
OpenCode Zen and Go.md`). The shared OpenCode credential is the same bearer
on both gates: `bid=e574f38a` in both banners' `bearer-new` lines.

### 2.2 Live knob table (read 19/10/2026 ~19:15–19:30 BRT)

Values verified from the **merged unit environment**
(`systemctl --user show <unit> -p Environment`), the **drop-in files**
(`~/.config/systemd/user/<unit>.service.d/`, `stat` for mtimes) and the
**service journals** (banners and `max_hold=` fast-fail lines).

| Knob | go `:8770` | zen `:8774` | zai `:8766` | mimo `:8773` |
|---|---|---|---|---|
| Hard cap `CLAUDE_API_THROTTLE_MAX` | 1024 (drop-in `99-max-concurrent.conf`) | 1024 (same) | 12 | 20 |
| `THROTTLE_AIMD_INITIAL_CONCURRENT` | 12 | 12 | 12 | 20 |
| AIMD floor `THROTTLE_AIMD_MIN` | **2** — live (drop-in `98-aimd-floor2.conf`, mtime 19:10:01.68; restart 19:10:02) | **1** (code default; no drop-in) | 1 (default) | 2 (unit env) |
| Hold `THROTTLE_MAX_HOLD_RETRY_AFTER_S` | **240** — live (drop-in `99-hold240.conf` mtime 18:48:22.91; restart 18:48:57; merged env shows 240) | **60 live, 240 pending** — drop-in exists (mtime 18:48:22.92) but the unit was last restarted 11:31:00, *before* the drop-in; live value confirmed as 60 by the `max_hold=60.0` fast-fail line at 18:27:09 | **60 live** — no drop-in; `max_hold=60.0` line at 12:17:47 | **240** — drop-in + restart 18:48:24 |
| Queue wait `THROTTLE_QUEUE_MAX_WAIT_S` | 180 | 180 | 180 | 180 |
| Queue mode | fair | fair | fair | fair |
| `Retry-After` advertised cap | 300 (code default `QUEUE_RETRY_AFTER_MAX_S`, `config.py:353`; observed `retry_after_s=300` at 19:02:26) | 300 (default) | 300 (default) | 300 (default) |
| `THROTTLE_UPSTREAM_SOCK_READ_TIMEOUT_S` | 120 | 120 | 120 | 120 |
| Priority reserve slots | not set | not set | 2 | 0 |
| Pushback retries | 1 (observed `retry=1/1`, 18:41:27) | unknown | unknown | 2 (unit env) |
| Retry-After state file | none (in-memory latch) | none | env set, **file absent on disk** (§8) | present (`retry-after-state.json`, 31 B, 06/10) |

**Discrepancy vs the shared mission brief** — the brief says
`THROTTLE_MAX_HOLD_RETRY_AFTER_S=240` on all four gates. As of this reading
that is true only for **go** and **mimo**; **zen's 240 drop-in has never been
activated** (no restart since 11:31) and **zai has no 240 at all**. The
cheap live check for any gate: the `max_hold=` field of its own
`retry-after-fast-fail` log lines, or the merged env as above.

### 2.3 What the knobs mean (code)

- `MAX_HOLD_RETRY_AFTER_S` (`config.py:296`, default 60) caps only
  **synthetic** pauses the gateway invents locally (budget pressure →
  `AIMD_BACKOFF_S`, concurrency pushback → `CONCURRENCY_COOLDOWN_S`, both
  clamped; `proxy.py:3780-3808`). A **real** upstream `Retry-After` is
  honored *uncapped* and, when it exceeds the ceiling, the request is
  fast-failed with `429` + the real `Retry-After` instead of held
  (`proxy.py:3911-3924`).
- `AIMD_MIN` (`config.py:151`) is the floor the shrink walks toward;
  `AIMD_DECREASE=0.7` is the multiplicative factor (`config.py:260`).
- Shrink triggers are statuses `{429, 503}` (`config.py:262`) **minus** three
  exemptions, each logged when taken: central-relayed queue timeouts
  (`proxy.py:3941-3953`, `queue-timeout-relay … (no aimd shrink)`), OAuth
  entitlement refusals (`proxy.py:3956+`), and Z.ai plan-quota gates
  (`_aimd_skip_zai_quota_gate`). `529` (upstream overloaded) is counted
  separately and never shrinks (`config.py:266`).
- Growth: any 2xx/3xx is `_aimd_grow` (`proxy.py:4073`), additive, rate-limited
  by `THROTTLE_RAMP_AFTER` successes past the backoff cooldown.

---

## 3. The client contract (pi)

| Client limit | Value | Receipt |
|---|---|---|
| pi HTTP idle timeout | **300 000 ms** default, not overridden on this host | pi 1.1.0 `dist/core/settings-manager.js:692`
  (`httpIdleTimeoutMs` → `DEFAULT_HTTP_IDLE_TIMEOUT_MS`),
  `dist/core/http-dispatcher.d.ts:1` (`DEFAULT_HTTP_IDLE_TIMEOUT_MS = 300000`); `~/.pi/agent/settings.json` contains no `httpIdleTimeoutMs` |
| pi turn retries | `retry.enabled=true`, **`maxRetries: 4`** | `~/.pi/agent/settings.json` (verbatim read 19/10) |
| pi-queue-wait extension budgets (zai `:8766` / mimo `:8773` only) | keeps a stamped queue-`503` pending up to 32 rejections and 1 800 000 ms of accumulated sleep; sleeps `Retry-After` + 1–1000 ms jitter, **fallback 15 s** when no `Retry-After` | `clients/pi-queue-wait/README.md:42` (15 s fallback); `clients/pi-queue-wait/queue-wait.mjs:35,210` (ports 8766/8773); budgets `specs/279-team-capacity/queue-expiry.md:13` |

**Consequence for the free gates**: the pi-queue-wait extension does **not**
intercept `:8770` or `:8774` — go/zen panes ride pi's **native** 4× retry
only. The stamped `503` body those gates emit is
`proxy queue wait exceeded; slots saturated — retrying will re-enter the
fair queue` (`proxy.py:826`) with `Retry-After` floored at 5 s and capped at
300 s (`README.md:164`). The exact native-retry delay schedule on the
`openai-completions` path is **not verified** — see §8; the "4×15 s" cadence
quoted in the mission brief is consistent with `maxRetries=4` plus the 15 s
missing-`Retry-After` fallback but was not confirmed in pi's source this pass.

Server-side, each tier also honors a forwarded end-to-end wait budget
(`x-anthropic-throttle-wait-budget-ms` → `_effective_queue_max_wait`,
`proxy.py:831`), so local and central waits cannot stack past the client's
patience (CLAUDE.md, "Bounded queue wait").

---

## 4. Pushback signal dictionary

Every line below is emitted by `throttler-gateway`/`anthropic-throttle-proxy`
to the journal as `[anthropic-throttle] <signal> …`. Fields shown are the ones
that matter for triage.

| Signal | Meaning | Proxy behaviour |
|---|---|---|
| `queue+ … inflight=N queued=M` | request parked in the fair queue | waits for a slot, bounded by `THROTTLE_QUEUE_MAX_WAIT_S` |
| `start … model=… lane=normal inflight=N` | slot acquired, request dispatched upstream | slot held for the whole generation |
| `done … status=200 elapsed_ms=… served=S disc=D retries=R` | request finished | 2xx feeds `_aimd_grow` |
| `rate-pushback-retry bid=… status=429 retry=1/1 pause=30.0 retry_after=0.0 synthetic_pause=True` | upstream 429 on dispatch; `synthetic_pause=True` = no real `Retry-After` was sent — the pause is the gateway's own cooldown (budget-pressure → up to 30 s, concurrency → 2 s), clamped to `MAX_HOLD_RETRY_AFTER_S` | retried once (mimo: twice) after the pause |
| `aimd-shrink bid=… status=429 max_concurrent=K … (budget/real-retry-after pushback)` | multiplicative decrease applied; **`max_concurrent` is the NEW cap after the shrink** (60→42→29 = ×0.7, zen 18:27:09-10) | cap walks toward `AIMD_MIN` |
| `retry-after-fast-fail bid=… source=pushback|pre-dispatch remaining=R max_hold=H` | a **real** upstream `Retry-After` (`remaining` seconds) exceeds the hold ceiling `H`; the client gets `429` + `Retry-After: R` immediately instead of a held request. `source=pre-dispatch` = refused before touching upstream while the window is latched | `proxy.py:3911-3924` |
| `queue-wait-timeout … reason=elapsed max_wait_s=179.98 service_s=… est_wait_s=E retry_after_s=A` | a **parked** request hit its 180 s budget; `503` + stamps + `Retry-After: A` (drain estimate, ≤300) | `proxy.py:808-830` |
| `queue-depth-reject … slots=S busy=B queued=Q ahead=A service_s=E est_wait_s=E2 max_wait_s=179.99 max_depth=0` | **pre-queue admission refusal**: the drain arithmetic says this arrival would wait past its budget, so it is refused *before* parking; the terminal line is `queue-wait-timeout reason=pre-queue-depth` | `proxy.py:5911+`, `tests/test_queue_depth_admission.py` |
| `client-disconnect … no_upstream_retry=true` | the client hung up mid-request; the gate does **not** replay upstream | `proxy.py:742-751` |
| `bearer-new bid=… max_concurrent=N hard_max=1024` | bearer (re)registration; `N` is the AIMD cap it starts/resumes with | fresh start ⇒ `THROTTLE_AIMD_INITIAL_CONCURRENT` |
| `retry-probe-begin/finish bid=… result=open` | post-latch probe of the credential; `result=open` = the bearer is usable again | observed after both go-gate restarts (18:48:59→18:49:37, 19:10:06→19:10:10) |
| `queue-timeout-relay … (no aimd shrink)` | relayed central queue `503` — admission backpressure, exempt from shrink | `proxy.py:3941-3953` |
| `oauth-entitlement-refusal … (no aimd shrink, no pause)` | entitlement gate masked as 429 — exempt | `proxy.py:3956+` |
| `retry-after-fast-fail … remaining≈90000` (zai 12:17:47: `remaining=90126`) | the plan window is locked upstream for hours — fast-fail is *correct*; restarting does not clear it | real `Retry-After` is honored uncapped |

Observability endpoints on every gate: `GET /__throttle/health` (per-bearer
`live_cap` ≥ floor, contract `specs/002-delivery-platform/contracts/health-json.md`),
`GET /__throttle/admission`, `POST /__throttle/quiesce` and
`/__throttle/resume` (`proxy.py:6787-6790`).

---

## 5. First-response playbook — "panes locked in retry loops"

Worked example: the 19/10/2026 go-gate incident, replayed with receipts.

### 5.1 Identify the gate and read the signal

The pane's `PI_PROVIDER` says which gate it uses (`opencode-go` → `:8770`,
`opencode` → `:8774`). Then:

```sh
journalctl --user -u opencode-go-throttle-proxy -o short-iso --since '-15min' \
  | grep -E 'aimd-shrink|queue-wait-timeout|queue-depth-reject|retry-after-fast-fail|client-disconnect' | tail -40
```

### 5.2 Classify what you see

**(a) Aimd-shrink pinned at the floor + pre-queue-depth storm = the 19/10
incident pattern.** Receipts:

```
18:41:27 aimd-shrink bid=e574f38a status=429 max_concurrent=1 retry_after=0.0 pause=30.0 synthetic_pause=True (budget/real-retry-after pushback)
18:53:48 / 18:56:01 / 18:57:24  aimd-shrink … max_concurrent=1  (still pinned)
19:02:24 queue-wait-timeout … reason=elapsed max_wait_s=179.984 inflight=1 service_s=171.45 source=inflight est_wait_s=342.901 retry_after_s=300
19:02:26 queue-depth-reject … slots=1 busy=1 queued=1 ahead=1 service_s=173.535 est_wait_s=347.07 max_wait_s=179.991 max_depth=0
19:02:26 queue-wait-timeout … reason=pre-queue-depth … est_wait_s=347.07 retry_after_s=300
19:02:30 queue-depth-reject … est_wait_s=355.21 …
…
18:59:31+ client-disconnect … no_upstream_retry=true   (pi panes died in retry loops)
```

Mechanism: the `429` shrank the shared bearer to the floor; the single slot
was held by a **171–303 s generation**; with one slot the drain estimate
(≈2× the in-flight service time) blew past the 180 s budget, so every other
arrival was either timed out at 180 s or refused *before* queueing — each
turn paying a full wait first, then a `503 Retry-After: 300`, then pi's
4 native retries, then dying. One slow generation starved the whole fleet.

**(b) Fast-fail flood with huge `remaining`** (seconds to tens of thousands)
= the plan window is locked upstream (zai receipt: `remaining=90126`,
12:17:47). Failing fast is the correct behaviour; a restart does **not** help
— the real `Retry-After` is re-observed on the next request. Check the
provider's console/plan state instead.

**(c) Pure queue-wait-timeouts with a healthy cap** = genuine saturation at
the current width. Do nothing destructive; the queue is doing its job and
width will re-grow (§6a).

### 5.3 Act — recovery paths, in order of preference

- **(a) Wait it out (preferred).** With the go gate's floor now at 2 and
  hold at 240 s, a stuck 1-slot state is bounded: new arrivals are refused
  *cleanly* with `Retry-After` instead of parking 180 s first (the
  pre-queue-depth refusal is the designed failure mode, not a bug). The
  171–303 s generation finishes, and additive growth resumes.
- **(b) Manual ceiling reset via restart** — used twice on 19/10:
  `systemctl --user restart opencode-go-throttle-proxy`.
  Receipts: `Stopped` → `Started` (18:48:24→18:48:57; 19:10:02),
  then `listening … max_concurrent=1024`, `retry-probe-begin` →
  `retry-probe-finish … result=open`, `bearer-new bid=e574f38a
  max_concurrent=12 hard_max=1024` (19:10:06). This re-registers the bearer
  at the AIMD initial (12) and clears the in-memory latch.
  **Costs**: (i) in-flight turns are cut — the drain window
  `THROTTLE_SHUTDOWN_TIMEOUT_S=85` (default; not overridden on these units,
  `config.py:47`) held the 18:48 restart for 33 s before releasing it;
  restart only when the panes are already dead. (ii) On go/zen there is no
  `THROTTLE_RETRY_AFTER_STATE_FILE`, so the reset clears local state only —
  a real upstream window returns immediately on the next dispatch. (iii) The
  fresh AIMD cap of 12 is *earned* width only after ~10 successes per step;
  expect a few quiet minutes.
- **(c) Bearer probe-up** — the `retry-probe-begin/finish result=open`
  sequence after a restart *is* the probe path; no manual credential
  action was needed on 19/10 (probe open in 4 s at 19:10:06-10; 38 s at
  18:48:59→18:49:37).
- **(d) Planned quiet window**: `POST /__throttle/quiesce` /
  `/__throttle/resume` on the gate rather than killing the service.

### 5.4 Verify after acting

```sh
systemctl --user show opencode-go-throttle-proxy -p Environment   # knobs
curl -s http://127.0.0.1:8770/__throttle/health                   # live_cap ≥ floor
curl -s http://127.0.0.1:8770/__throttle/admission
```

Success looks like: `bearer-new … max_concurrent=12 hard_max=1024`,
`retry-probe-finish result=open`, then `done status=200` lines with growing
`served=` and **no** `aimd-shrink` for ≥10 min.

---

## 6. Recovery mechanics (reference)

- **(a) Passive re-growth** — every 2xx/3xx grows the cap additively
  (`_aimd_grow`, `proxy.py:4073`), one step per `THROTTLE_RAMP_AFTER` (10)
  successes past the `THROTTLE_BACKOFF` cooldown. This is the only path that
  *earns* width from the upstream's own tolerance.
- **(b) Probe-up** — after a latched window, the gate probes the credential
  before dispatch (`_pre_dispatch_probe_gate`, `proxy.py:4752`;
  `retry-probe-*` receipts above). `result=open` reopens dispatch.
- **(c) Restart = manual ceiling reset** — §5.3(b). Equivalent knob order:
  write the drop-in → `systemctl --user daemon-reload` → `restart` → verify
  the `listening` banner and `bearer-new` line.
- **(d) Fast-fail wall** — while a real upstream `Retry-After` is active,
  `retry-after-fast-fail` lines every second are *normal* (zai shows one per
  second under a plan lock). They are cheap refusals, not load; the client
  is expected to honour `Retry-After`.

---

## 7. Operating checklist (do / don't)

**Do**

- Keep `THROTTLE_QUEUE_MODE=fair` and `THROTTLE_QUEUE_MAX_WAIT_S=180` on all
  four gates (live today on all four).
- Keep the client ladder: 180 (queue) < 240 (hold) < 300 (pi idle).
- Keep go/mimo floors at 2; treat any further floor raise as a
  cross-service change requiring evidence of tolerated width.
- Read `max_hold=` from the gate's own fast-fail lines (or the merged env)
  when verifying a hold change — the drop-in file alone proves nothing until
  the unit restarts (zen's 240 is on disk but not live since 11:31).
- Restart go/zen only when panes are already dead, and verify the
  `listening`/`bearer-new`/`retry-probe-finish` receipts after.

**Don't**

- Don't hand-raise a live ceiling or set `THROTTLE_AIMD_MIN=0` (clamped to
  1 anyway; `config.py:151`).
- Don't raise `THROTTLE_QUEUE_MAX_WAIT_S` past pi's 300 s idle timeout
  (`specs/279-team-capacity/queue-expiry.md:32`).
- Don't read `queue-depth-reject` as a fault to suppress — it is the gate
  refusing a doomed wait *before* the client pays 180 s for it.
- Don't chase a real-`Retry-After` wall with restarts (§5.2b).
- Don't infer "hold=240 is configured" from the presence of a drop-in file.

---

## 8. Unknowns / open items (explicitly unverified, 19/10/2026)

1. **pi native-retry delay schedule** on the `openai-completions` path (the
   "15 s" in the brief's "4×15 s"). Verified: `maxRetries=4`
   (`~/.pi/agent/settings.json`). Not verified: the inter-retry delay constant
   in pi 1.1.0's bundle; the only documented 15 s is the pi-queue-wait
   extension fallback, which does **not** serve `:8770`/`:8774`.
2. **zen hold=240 activation** — the drop-in exists; the unit has not been
   restarted since 11:31, so the live value is still 60. Whoever restarts
   zen next flips it; until then the "240 on all four gates" claim is false
   for zen (and zai).
3. **zai `THROTTLE_RETRY_AFTER_STATE_FILE`** points to
   `~/.local/state/zai-throttle-proxy/anthropic-throttle-proxy/retry-after-state.json`,
   but the file is absent (directory listing 19/10: only
   `overrides.json.incident-300-20260829-do-not-restore`). Whether/when the
   latch is ever persisted on zai is unverified.
4. **Upstream free-lane policy**: the concurrency/throughput at which
   `opencode.ai/inference[/go]` starts answering 429 for `*-free` models is
   not documented anywhere in this repo or the vault; it is only observable
   indirectly via `aimd-shrink` events. zen had grown to `max_concurrent=60`
   when a **real** `Retry-After: 9171` (≈2 h 33 min) 429 hit at 18:27:09 —
   so the sustainable free width on zen is somewhere below 60 and above 12;
   exact value unknown.
5. **`THROTTLE_RATE_PUSHBACK_RETRIES` defaults** on go/zen/zai: go observed
   `retry=1/1` today (so effective 1); zen/zai not observed in this pass.
6. **Why the 18:48 probe took 38 s vs 4 s at 19:10** — both `result=open`;
   the gate banner advertises `egress_probe_timeout_s=10.0`. Not diagnosed
   (non-blocking).

---

## Appendix A — incident timeline, 19/10/2026 (go gate `:8770`)

All lines verbatim from
`journalctl --user -u opencode-go-throttle-proxy -o short-iso`
(pid 2457560 → 2599264 → 2736866 across the two restarts).

| Time (BRT) | Event |
|---|---|
| 18:40:54–18:41:27 | normal `done status=200` traffic; one long `mimo-v2.6-flash` (76.9 s) |
| 18:41:27 | `rate-pushback-retry … status=429 retry=1/1 pause=30.0 synthetic_pause=True` → `aimd-shrink … max_concurrent=1` (floor was 1 then; `98-aimd-floor2.conf` did not exist yet) |
| 18:48:22.91 | drop-in `99-hold240.conf` written (go/zen/mimo, same second) |
| 18:48:24→18:48:57 | go gate restart (33 s graceful drain) → `listening … max_concurrent=1024` |
| 18:48:59→18:49:37 | `retry-probe-begin` → `bearer-new bid=e574f38a max_concurrent=12 hard_max=1024` → `retry-probe-finish result=open` |
| 18:53:48 / 18:56:01 / 18:57:24 | `aimd-shrink … max_concurrent=1` ×3 — the post-restart cap collapsed again (floor still 1) |
| 18:59:31+ | `client-disconnect … no_upstream_retry=true` — pi panes aborting |
| 19:02:24 | `queue-wait-timeout reason=elapsed … service_s=171.45 est_wait_s=342.901 retry_after_s=300` |
| 19:02:26–19:03:54 | `queue-depth-reject` / `queue-wait-timeout reason=pre-queue-depth` storm (cids `:50724 :60946 :49328 :60976 :60988 …`, `est_wait_s` 182–355, `retry_after_s` 183–300) |
| 19:10:01.68 | drop-in `98-aimd-floor2.conf` written (`THROTTLE_AIMD_MIN=2`) |
| 19:10:02→19:10:10 | go gate restart → `listening` → `retry-probe-begin` → `bearer-new max_concurrent=12 hard_max=1024` → `retry-probe-finish result=open` (probe open in 4 s) |
| 19:10+ | merged env live: `CLAUDE_API_THROTTLE_MAX=1024 THROTTLE_AIMD_MIN=2 THROTTLE_MAX_HOLD_RETRY_AFTER_S=240 THROTTLE_QUEUE_MAX_WAIT_S=180` (verified via `systemctl --user show … -p Environment`, ~19:15) |

## Appendix B — commands used for this document

```sh
systemctl --user cat opencode-{zen,go}-throttle-proxy.service zai-throttle-proxy.service mimo-throttle-proxy.service
systemctl --user show <unit> -p Environment
journalctl --user -u opencode-go-throttle-proxy -o short-iso --since "2026-10-19 18:40:00" --until "2026-10-19 19:25:00"
stat -c '%y %n' ~/.config/systemd/user/<unit>.service.d/*.conf
curl -s http://127.0.0.1:8770/__throttle/health    # playbook command; not executed while writing
rg -n "AIMD_STATUSES|MAX_HOLD_RETRY_AFTER_S|QUEUE_MAX_WAIT_S" src/anthropic_throttle_proxy/config.py
```
