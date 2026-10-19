# Incident 2026-10-19 — opencode FREE-lane collapse (go gate :8770)

**Status:** mitigated, residual risks open
**Gate:** `opencode-go-throttle-proxy.service`, `127.0.0.1:8770`, upstream `https://opencode.ai/inference/go`
**Shared bearer:** `bid=e574f38a`
**Written:** 2026-10-19 (evening, BRT)
**Author:** slice agent, incident retrospective

All times are BRT (UTC−3), as emitted by `journalctl -o short-iso`. Every
factual claim below carries a receipt; the reproduction commands are in
[§7](#7-evidence-reproduction).

> **Reading note on source line references.** This checkout carries uncommitted
> work from sibling slices (`git status`, 19/10/2026 ~19:50: `M
> src/anthropic_throttle_proxy/limiter.py`, `M .../metrics.py`, `M
> .../proxy.py`, plus the untracked `free_budget.py` / `free_gate_watchdog.py`
> and the `specs/333-*`, `specs/334-*` trees). All `*.py:NNN` anchors below
> point at that **working tree**, which is what a reviewer sees. Two symbols
> cited here are new in it and absent from `HEAD`: `FairBearerLimiter.aimd_floor`
> (`limiter.py:815`) and the free-gate watchdog module. The *behaviour* in §3 is
> the behaviour of the deployed build, which predates them.

---

## 1. Executive summary

The go gate's AIMD ceiling for bearer `e574f38a` collapsed from 64 to the floor
value 1 between **16:47:10** and **16:52:28** on a run of **headerless upstream
429s**, and stayed pinned at 1 for roughly two hours. With the ceiling at 1 and
free-model generations running **171–304 s**, the pre-queue-depth admission
estimator could no longer promise any second concurrent request a start inside
the 180 s `queue_max_wait`, so it refused admission with a **503** instead of
queuing. That produced **16 queue-timeout 503s across the day** — two single
early ones (17:01:57, 17:24:55), a cluster of 4 at **17:57:24–17:57:39** (3
clients), and the main storm of 10 at **19:02:24–19:03:03** (8 clients) — and
at least one pi agent pane died in the retry loop.

A first proxy restart at **18:48:57** did not fix anything: the fresh process
re-registered the bearer at 12 and re-collapsed to 1 within 4 min 51 s. The
storm hit after that restart. Two mitigations are now live: `THROTTLE_AIMD_MIN=2`
on the go gate (drop-in at 19:10:01, restart 19:10:02) and
`THROTTLE_MAX_HOLD_RETRY_AFTER_S=240` on four gates (drop-ins at 18:48:22).

The lane recovered to `max_concurrent=17` by 19:19:57 and to **59** by 19:48:27
(sustained `aimd-grow` from 19:33:16, no shrinks in that stretch).

> **Scope correction to the original incident framing.** The collapse did not
> begin at 18:57. The first 429 of the day on this gate was at **16:47:10** and
> the ceiling reached 1 at **16:52:28**; 18:57:24 was simply the last of 15
> re-confirmations while already pinned at 1. See [§3](#3-timeline).

---

## 2. Root cause chain

Each step is a code fact plus a log fact.

**2.1 — Upstream returned 429s with no `Retry-After` and no unified headers.**

```
2026-10-19T16:47:10-03:00 rate-pushback-retry bid=e574f38a status=429 retry=1/1 pause=30.0 retry_after=0.0 synthetic_pause=True
2026-10-19T16:47:10-03:00 aimd-shrink   bid=e574f38a status=429 max_concurrent=44 retry_after=0.0 pause=30.0 synthetic_pause=True (budget/real-retry-after pushback)
```

`retry_after=0.0` + `synthetic_pause=True` on every one of the 58 go-gate shrinks
of the day. Contrast the zen gate, which got *real* `Retry-After` headers on the
same bearer (§6.3).

**2.2 — On a gate with no meter evidence, a headerless 429 is hard-wired to read
as "budget".**

Live confirmation that the go upstream carries no unified headers at all:
`GET http://127.0.0.1:8770/__throttle/health` returns
`"bearers":{"e574f38a":{"unified":null,"unified_at":null,"last_ratelimit":null}}`
(sampled 19:49). With no response headers and no cached sample, the
classification cannot resolve to anything but budget.

`_budget_under_pressure` (`src/anthropic_throttle_proxy/proxy.py:3714`) falls
through three branches when a 429 carries no unified headers and the cached
unified state is absent/stale:

```python
if not config.METER_BINDINGS and not config.METER_BINDING_REQUIRED:
    return _plan_lane_siblings_have_headroom(now)   # proxy.py:3642 -> :3679
...
lane_id = config.PLAN_METER_LANE
if not lane_id:
    return False                                     # proxy.py:3693
```

and the caller inverts it:

```python
return not _plan_lane_has_headroom(bid)              # proxy.py:3770
```

The go gate's unit has **no** `THROTTLE_METER_BINDINGS` and **no**
`THROTTLE_PLAN_METER_LANE` (verified: `systemctl --user cat
opencode-go-throttle-proxy.service | grep -iE 'METER|BINDING'` → no output).
`_parse_meter_bindings("")` returns `({}, True)` (`config.py:190`), so
`METER_BINDINGS` is empty and `_plan_lane_siblings_have_headroom` returns `False`
at the `if not lane_id` guard. Result: `_budget_under_pressure` → **True**,
`is_concurrency_429` → **False** (`proxy.py:4034`), and every headerless 429
takes the `aimd-shrink` branch inside `_aimd_apply_pushback`
(`proxy.py:4013`) with a 30 s
`AIMD_BACKOFF_S`-derived synthetic pause (`config.py:155`).

**2.3 — Multiplicative decrease drove the ceiling to the floor.**

`FairBearerLimiter.shrink` (`limiter.py:838`) applies `AIMD_DECREASE = 0.7`
(`config.py:260`) floored through `self.aimd_floor()` (`limiter.py:815`, whose
docstring cites *spec 334, 19/10/2026 go-gate incident*; the expression is
`new_max = max(self.aimd_floor(), min(scaled, self.max_concurrent - 1))` at
`limiter.py:857`, resolving to `config.AIMD_MIN` at `config.py:151`). The go
gate had no `THROTTLE_AIMD_MIN` drop-in until 19:10:01, so the floor was the
compiled default **1**. Nine shrinks in 4 min 18 s took it there:

```
16:47:10 mc=44 → 16:47:50 mc=30 → 16:48:29 mc=21 → 16:49:09 mc=14
       → 16:49:49 mc=9  → 16:50:28 mc=6  → 16:51:09 mc=4
       → 16:51:49 mc=2  → 16:52:28 mc=1
```

The ceiling immediately before the first shrink was **64** (`aimd-grow ...
max_concurrent=64` at 16:42:58), and `int(64 * 0.7) == 44` — the arithmetic
matches the logged value exactly.

**2.4 — At ceiling 1 the admission estimator cannot admit a second request.**

`_check_queue_admission` (`limiter.py:1175`) rejects before enqueueing when the
drain estimate exceeds `max_wait`:

```
queue-depth-reject bid=e574f38a cid=127.0.0.1:46748 slots=1 busy=1 queued=0
  ahead=0 service_s=210.241 source=inflight est_wait_s=210.241
  max_wait_s=179.993 max_depth=0
```

(the `queue-depth-reject` line is emitted at `limiter.py:1193`)

With one slot busy for `service_s` seconds, `est_wait_s ≈ service_s`; a single
171–304 s generation already exceeds the 180 s `THROTTLE_QUEUE_MAX_WAIT_S`
(go-gate unit env). The caller then writes the 503 via
`_queue_wait_timeout_response` (`proxy.py:773`) with `status=503`, a
`retry-after` header, and `reason=pre-queue-depth` in the log
(`proxy.py:813`).

**2.5 — The 503s are never counted as `done`.**

`grep -c 'status=503'` over the whole 17:30–19:20 go-gate window returns **0**.
Queue-timeout 503s are answered before the request enters the served counter, so
503 volume must be read from `queue-wait-timeout` lines, not from `done`.
This is a real observability trap and is why the storm looked like "quiet"
traffic in any dashboard keyed on `done` status.

**2.6 — Restart #1 re-created the same state.**

The 18:48:57 restart reset the bearer to `bearer-new bid=e574f38a
max_concurrent=12`, but the upstream kept returning headerless 429s, so the
fresh process re-collapsed `8 → 5 → 3 → 2 → 1` between 18:49:02 and 18:53:48.
A restart is not a fix while the upstream signal persists.

---

## 3. Timeline

### 3.1 Build-up (11:31 – 16:47)

| Time | Event | Receipt |
|---|---|---|
| 11:31:01 | go gate process starts, `listening on 127.0.0.1:8770 max_concurrent=1024 queue_mode=fair upstream=https://opencode.ai/inference/go` | journald |
| 11:31:02 | `bearer-new bid=e574f38a max_concurrent=12 hard_max=1024 queue_mode=fair` | journald |
| 11:38:22 → | steady `aimd-grow`, one step per successful completion | journald |
| (peak) | `aimd-grow ... max_concurrent=64` (highest value observed all day) | journald |
| 16:47:10 | **first 429 of the day**, headerless → `aimd-shrink max_concurrent=44` | journald |

Note the load at the moment of the first 429: `inflight=2`, `queued=0`,
`served=519`. The lane was using ~3 of its ~63 slots — the 429 was upstream-side,
not caused by a local concurrency burst.

### 3.2 Collapse (16:47 – 16:52)

```
16:47:10 mc=44      16:47:50 mc=30      16:48:29 mc=21      16:49:09 mc=14
16:49:49 mc=9       16:50:28 mc=6       16:51:09 mc=4       16:51:49 mc=2
16:52:28 mc=1  <-- floor (THROTTLE_AIMD_MIN default 1)
```

All nine carry `retry_after=0.0 pause=30.0 synthetic_pause=True
(budget/real-retry-after pushback)`.

### 3.3 Pinned at 1, first 503s and first cluster (16:52 – 18:48)

| Time | Event |
|---|---|
| 16:53:09 … 17:16:49 | 8 further `aimd-shrink ... max_concurrent=1` (re-confirmations at 16:53:09, 16:54:15, 16:54:54, 16:56:09, 16:56:49, 17:05:17, 17:05:56, 17:16:10, 17:16:49) |
| **17:01:57** | **first 503 of the day**: `queue-wait-timeout cid=127.0.0.1:37994 reason=elapsed max_concurrent=1 est_wait_s=182.755` |
| **17:24:55** | second 503: `queue-wait-timeout cid=127.0.0.1:39944 reason=elapsed max_concurrent=1 est_wait_s=182.842` |
| 17:34:03 / 17:34:43 | shrink to 2 then back to 1 |
| 17:41:32 … 17:54:26 | partial recovery `2 → 3 → 4 → 5`, then `3 → 2` |
| **17:57:24 – 17:57:39** | **first 503 cluster**: 4 × `queue-wait-timeout` to 3 distinct clients (`cid 47962` ×2, `38916`, `38930`), `est_wait_s` 182.6 / 184.7 / 188.7 / 196.8 vs `max_wait_s` 179.985–179.996, `inflight=2`, `max_concurrent=2` |
| 18:01:25 → 18:19:15 | client-disconnect run: `disc` 12 → 29 (17 × 499 in 18 min) |
| 18:22:26 → 18:48:19 | pinned at 1, 15 further re-confirmations |

Both early 503s were `reason=elapsed` (a queued request blew its 180 s budget),
not `pre-queue-depth` (refused before entering). Same estimator, different
call site — see `proxy.py:773`.

### 3.4 Restart #1 and re-collapse (18:48 – 18:58)

| Time | Event |
|---|---|
| 18:48:22 | `99-hold240.conf` written to all four gates (see §5) |
| 18:48:24 | `Stopping Local queue/concurrency proxy for the OpenCode go gateway...` |
| 18:48:57 | `Stopped`, accounting: `Consumed 11min 21.916s CPU over 7h 17min 56.656s wall, 420.8M memory peak, 2.2M swap peak` |
| 18:48:57 | `Started` + `listening on 127.0.0.1:8770` |
| 18:48:59 | `retry-probe-begin` + `bearer-new bid=e574f38a max_concurrent=12` |
| 18:49:02 | `aimd-shrink max_concurrent=8` |
| 18:49:37 | `retry-probe-finish result=open` |
| 18:50:24 | `aimd-shrink max_concurrent=5` |
| 18:51:32 | client-disconnect, `longcat-2.5-preview-free`, `elapsed_ms=11440` |
| 18:51:44 | `aimd-shrink max_concurrent=3` |
| 18:53:15 | `aimd-shrink max_concurrent=2` |
| **18:53:48** | `aimd-shrink max_concurrent=1` — re-pinned |
| 18:55:09 | client-disconnect, `longcat-2.5-preview-free`, `elapsed_ms=50597` |
| 18:56:01 | `aimd-shrink max_concurrent=1` |
| **18:57:24** | `aimd-shrink max_concurrent=1` — the event the original framing named |
| 18:57:20 *(zen gate :8774)* | **real** upstream 429, `retry_after=7361.0`, `synthetic_pause=False` → `aimd-shrink max_concurrent=8`, `retry-after-fast-fail ... remaining=7361 max_hold=240.0`, `retry-probe-finish result=closed` |
| 18:59:32 | the request that will hold the only slot starts: `cid=127.0.0.1:53614 model=mimo-v2.6-flash` |
| 18:59:33 | client-disconnect, `cid=127.0.0.1:53604`, `elapsed_ms=8730` |

### 3.5 The 503 storm (19:02 – 19:04)

10 `queue-wait-timeout` responses to **8 distinct client connections**, all with
`max_concurrent=1 inflight=1`:

| Time | cid | reason | service_s | est_wait_s | retry_after_s |
|---|---|---|---|---|---|
| 19:02:24 | :50724 | elapsed | 171.45 | 342.901 | 300 |
| 19:02:26 | :50724 | pre-queue-depth | 173.535 | 347.07 | 300 |
| 19:02:30 | :60946 | pre-queue-depth | 177.605 | 355.21 | 300 |
| 19:02:33 | :49328 | elapsed | 180.082 | 180.082 | 181 |
| 19:02:35 | :49328 | pre-queue-depth | 182.135 | 182.135 | 183 |
| 19:02:38 | :60976 | pre-queue-depth | 185.68 | 185.68 | 186 |
| 19:02:39 | :60988 | pre-queue-depth | 186.164 | 186.164 | 187 |
| 19:02:47 | :51776 | pre-queue-depth | 194.201 | 194.201 | 195 |
| 19:02:54 | :37810 | pre-queue-depth | 201.748 | 201.748 | 202 |
| 19:03:03 | :46748 | pre-queue-depth | 210.241 | 210.241 | 211 |

Measured `est_wait_s` range: **180.082 – 355.21 s** against `max_wait_s` ≈ 180 s.

The single slot freed at **19:04:36**: `done status=200 cid=127.0.0.1:53614
model=mimo-v2.6-flash elapsed_ms=303640` — a **5 min 03.6 s** generation.

### 3.6 Mitigation restart and recovery (19:09 – 19:49)

| Time | Event |
|---|---|
| 19:09:03 | `aimd-grow max_concurrent=2` |
| 19:10:01 | `98-aimd-floor2.conf` written (`THROTTLE_AIMD_MIN=2`) |
| 19:10:02 | `Stopping` → `Stopped` → `Started` (restart #2) |
| 19:10:03 | `listening on 127.0.0.1:8770` |
| 19:10:06 | `retry-probe-begin` + `bearer-new bid=e574f38a max_concurrent=12` |
| 19:10:10 | `retry-probe-finish result=open` |
| 19:19:37 / :44 / :49 / :53 / :57 | `aimd-grow` 13 → 14 → 15 → 16 → 17 |
| 19:20:15 → 19:27:51 | **6 further headerless-429 shrinks** (`retry_after=0.0 synthetic_pause=True`): resting values 14, 23, 16, 17, 11, 12 — the input persists, but with `THROTTLE_AIMD_MIN=2` the floor never comes near 1 again (lowest resting value observed: **11**) |
| 19:33:16 → 19:48:27 | sustained `aimd-grow` 31 → 59, no shrink |
| 19:49 (sampled) | `GET /__throttle/health`: `inflight=7 queued=0 served=840 upstream_egress_ok=true`, bearer `e574f38a` `unified: null`, `unified_at: null`, `last_ratelimit: null`, `client_disconnects: 92` |

---

## 4. Blast radius

### 4.1 Confirmed — 503 rejections

All 16 `queue-wait-timeout` responses of the day, by cluster:

| Window | Responses | Distinct clients | `max_concurrent` | `est_wait_s` range |
|---|---|---|---|---|
| 17:01:57 | 1 (`reason=elapsed`, `cid :37994`) | 1 | 1 | 182.755 |
| 17:24:55 | 1 (`reason=elapsed`, `cid :39944`) | 1 | 1 | 182.842 |
| 17:57:24 – 17:57:39 | 4 (all `pre-queue-depth`) | 3 (`:47962` ×2, `:38916`, `:38930`) | 2 | 182.624 – 196.755 |
| 19:02:24 – 19:03:03 | 10 (8 `pre-queue-depth`, 2 `elapsed`) | 8 (`:50724` ×2, `:49328` ×2, `:60946`, `:60976`, `:60988`, `:51776`, `:37810`, `:46748`) | 1 | 180.082 – 355.21 |

Day totals: **16 `queue-wait-timeout` 503s** reaching **13 distinct client
connections**, plus **12 `queue-depth-reject`** log lines (the pre-admission
counterpart). For context the same day served **1315** requests at `status=200`
and logged **116** at `status=429`.

### 4.2 Confirmed — one pi agent pane terminated in the retry loop

Transcript
`~/.pi/agent/sessions/--home-notroot--/2026-10-19T19-58-14-875Z_01a155be-4358-7510-bbb6-50d5fb1e8c9b.jsonl`
(pid-time 16:58:14, last write **19:02:57.208**) recorded five consecutive
empty assistant turns carrying `provider: opencode-go`,
`model: longcat-2.5-preview-free`, containing the string
`slots saturated — retrying will re-enter the fair queue`, at:

```
2026-10-19T22:02:24.648Z   (= 19:02:24 BRT)
2026-10-19T22:02:26.724Z   (= 19:02:26 BRT)
2026-10-19T22:02:30.793Z   (= 19:02:30 BRT)
2026-10-19T22:02:38.869Z   (= 19:02:38 BRT)
2026-10-19T22:02:54.936Z   (= 19:02:54 BRT)
```

Those five client attempts map onto proxy `cid`s `:50724`, `:50724`, `:60946`,
`:60976`, `:37810`. The transcript's final entry is a `pi-artifact-cleanup`
`custom` record at 19:02:57.209; nothing was written after it.

This was the **only** pi session file modified between 19:01:30 and 19:10:00, and
the only session in that window containing the 503 body text. The remaining 5
rejections in the storm went to non-pi or non-transcript-logging clients.

The herdr seat manifest
(`~/.local/state/herdr-seat-manifest/side-projects-pre-stop-*.json`) maps this
transcript to pane **`w2A:p2Q`**, tab *wave-replay-gpt*.

### 4.3 Confirmed — client disconnects

`disc` counter on the pre-restart process:

```
17:30:00 disc=5  →  18:48:57 disc=34     (29 × 499 in 78 min)
```

Sub-runs: 17:49:40–17:54:01 → disc 6→11; **18:01:25–18:19:15 → disc 12→29** (17
disconnects in 18 min, the aftermath of the 17:57 cluster); 18:27–18:48 → disc
30→34. Post-restart #1: disc 0→3 (18:49:43, 18:51:32, 18:55:09, 18:59:33).
Post-restart #2: disc 0→5 by 19:19:58.

A 499 is `client-disconnect where=first ... exc='ClientConnectionResetError:
Cannot write to closing transport' no_upstream_retry=true` — the client hung up
while the proxy still held the request. Not every 499 is attributable to this
incident (agents abort turns on their own), so 29 is an upper bound, not a
count of incident-caused deaths. Whole-day `status=499` total on the pre-restart
process: **71**.

### 4.4 NOT attributable — the herdr roster churn at 18:57

The seat manifest shows 17 panes flipping to `state=replaced` between
`18:52:54` and `18:57:53`. Those panes were inspected individually and **none**
was on `opencode-go`:

```
w22:p25  present/openai-codex/gpt-6.1-sol   -> replaced
w2J:pH   present/zai/glm-5.3-flash          -> replaced
w2J:pM   present/openai-codex/gpt-6.1-sol   -> replaced
w2K:pP   present/zai/glm-5.3-flash          -> replaced
w3E:p3   present/zai/glm-5.3-flash          -> replaced
... (11 more zai / openai-codex, 0 opencode-go)
```

Recorded here explicitly so the timing coincidence is not later mistaken for
causation.

### 4.5 Inventory of the lane at the time

Per the 18:52:54 manifest, of 127 side-projects seats only 31 carried a live
identity: 13 `zai`, 12 `openai-codex`, 5 `opencode`, **1 `opencode-go`**
(pane `w2P:p1H`, `longcat-2.5-preview-free`, tab *MiMo package purchase* — the
incident-integrator seat). The go gate's own traffic volume (**874 lines** with
`bid=e574f38a` in 18:00–19:20, plus a second bearer `bid=35e5a963`; 4578 lines
all day) comes from pi seats whose identity the manifest does not resolve.

---

## 5. Mitigations applied

| # | Change | Evidence | Verified live |
|---|---|---|---|
| M1 | `THROTTLE_MAX_HOLD_RETRY_AFTER_S=240` drop-in `99-hold240.conf`, mtimes **18:48:22.910–.916**, on `anthropic-throttle-proxy`, `mimo-throttle-proxy`, `opencode-go-throttle-proxy`, `opencode-zen-throttle-proxy` | `find ~/.config/systemd/user -name 99-hold240.conf -printf ...` | yes — `systemctl --user cat <unit> \| grep MAX_HOLD` returns `Environment=THROTTLE_MAX_HOLD_RETRY_AFTER_S=240` for all four |
| M2 | `THROTTLE_AIMD_MIN=2` drop-in `98-aimd-floor2.conf`, mtime **19:10:01.682**, on `opencode-go-throttle-proxy` only | `stat -c '%y %n' .../98-aimd-floor2.conf` | yes — the go gate's first post-restart shrink floor is 2 (see §3.6 and R1) |
| M3 | Proxy restart #1 at **18:48:57** (with M1) | journald `Stopping`/`Stopped`/`Started` | no effect — re-collapsed to 1 by 18:53:48 |
| M4 | Proxy restart #2 at **19:10:02** (with M2) | journald | yes — recovered to 17 by 19:19:57 |

Config defaults that were in force before these drop-ins, for reference:
`AIMD_MIN=1` (`config.py:151`), `AIMD_BACKOFF_S=30` (`config.py:155`),
`AIMD_DECREASE=0.7` (`config.py:260`), `CONCURRENCY_COOLDOWN_S=2`
(`config.py:167`), `MAX_HOLD_RETRY_AFTER_S=60` (`config.py:296`),
`QUEUE_MAX_WAIT_S` env `THROTTLE_QUEUE_MAX_WAIT_S=180`.

**Port inventory actually measured** (the go gate is :8770; the zen gate is
:8774, not :8771):

| Unit | Port | Upstream |
|---|---|---|
| `anthropic-throttle-proxy` / `throttler-gateway` | 8765 | `https://api.anthropic.com` |
| `zai-throttle-proxy` | 8766 | `https://api.z.ai` |
| `opencode-go-throttle-proxy` | 8770 | `https://opencode.ai/inference/go` |
| `mimo-throttle-proxy` | 8773 | `https://token-plan-sgp.xiaomimimo.com` |
| `opencode-zen-throttle-proxy` | 8774 | `https://opencode.ai/inference` |

---

## 6. Residual risks

**R1 — `AIMD_MIN=2` defers the storm, it does not remove it.**
At ceiling 2 with a single ~171 s generation in flight, `est_wait_s ≈ service_s`
for the next arrival and the third concurrent request is still refused. The
17:57 cluster is the proof: the lane was at `max_concurrent=2` and still produced
4 rejections with `est_wait_s` 182–197 s. Post-mitigation the go gate *did*
shrink again at 19:20:15 (mc=14) and 19:24:56 (mc=16), so the same headerless-429
input persists. A structural fix needs either a floor derived from the measured
service time, or an admission policy that queues when `est_wait > max_wait`
rather than rejecting.

**R2 — Headerless 429 → budget is unconditional on any gate without meter
evidence.**

Not hypothetical: post-mitigation, at a ceiling in the fifties, the go gate
took **six more headerless-429 shrinks** between 19:20:15 and 19:27:51, all with
`retry_after=0.0 pause=30.0 synthetic_pause=True (budget/real-retry-after
pushback)`. The `concurrency-429` fast path never fired.

`_plan_lane_siblings_have_headroom` returns `False` at `if not lane_id`
(`proxy.py:3693`) and `_bound_lane_has_headroom` returns `False` at
`if not lane_id` (`proxy.py:3673`). Both the go gate and the zen gate have no
`THROTTLE_PLAN_METER_LANE` and no `THROTTLE_METER_BINDINGS`, so on both gates
every headerless 429 buys a multiplicative decrease plus a 30 s synthetic pause.
The `concurrency-429` fast path in `_aimd_apply_pushback`
(`proxy.py:4034`) is effectively unreachable on these two gates.

**R3 — `zai-throttle-proxy` (:8766) has no `99-hold240.conf`.**
Its only drop-in is `90-throttler-7236d96.conf` from 2026-10-03. It still runs
the compiled default `MAX_HOLD_RETRY_AFTER_S=60`. If the hold mitigation was
intended fleet-wide, this gate was missed.

**R4 — AIMD state is per-process and per-gate, but the upstream rate-limit domain
is shared.**
Bearer `e574f38a` sits behind **both** :8770 and :8774. During the incident the
two gates held completely different ceilings for the same account — the go gate
at **1** and the zen gate at **87** — while the upstream was rate-limiting the
account as a whole. Nothing propagates a shrink across gates.

**R5 — Recovery is success-gated and therefore slow.**
`_aimd_grow` (`proxy.py:4073`) advances one step per completed request. After
restart #2 the lane took **9 min 51 s** (19:10:06 → 19:19:57) to climb 12 → 17.
Under load that is fine; after an idle period it means a long window at a low
ceiling, which is exactly the window in which R1 bites.

**R6 — `MAX_HOLD_RETRY_AFTER_S=240` exceeds `QUEUE_MAX_WAIT_S=180`.**
A bearer can be held for up to 240 s while the client only grants 180 s of queue
patience, so a held lane still sheds load to 503s. The two knobs currently point
in opposite directions.

**R7 — opencode.ai is genuinely rate-limiting this account.**
The zen gate received two real `Retry-After` values on the same bearer:

```
18:27:09  status=429 retry_after=9171.0 synthetic_pause=False  (≈2h33m)
18:57:20  status=429 retry_after=7361.0 synthetic_pause=False  (≈2h03m)
```

with `retry-after-fast-fail ... remaining=7361 max_hold=240.0` and
`retry-probe-finish result=closed`. A ~2 h upstream penalty is not a local
configuration problem: no local tuning recovers that window. The mission's
"never cross the line" invariant was crossed on the zen lane at 18:27:09 and
again at 18:57:20.

**R8 — 503 volume is invisible to `done`-status dashboards.**
See §2.5. Any alerting keyed on `done ... status=` will not see this class of
storm.

---

## 7. Evidence reproduction

```bash
# Full go-gate window (907 lines, 18:00–19:20)
journalctl --user -u opencode-go-throttle-proxy \
  --since '2026-10-19 18:00' --until '2026-10-19 19:20' --no-pager -o short-iso

# AIMD trajectory for the whole day
journalctl --user -u opencode-go-throttle-proxy --since '2026-10-19 00:00' \
  --no-pager -o short-iso | grep -E 'aimd-(shrink|grow)|bearer-new|listening'

# The 503 clusters
journalctl --user -u opencode-go-throttle-proxy --since '2026-10-19 17:30' \
  --until '2026-10-19 19:10' --no-pager -o short-iso | grep -E 'queue-wait-timeout|queue-depth-reject'

# The zen gate's real upstream 429s
journalctl --user -u opencode-zen-throttle-proxy --since '2026-10-19 18:00' \
  --until '2026-10-19 19:20' --no-pager -o short-iso | grep -E 'status=429|aimd-shrink|retry-after-fast-fail'

# Client-side receipt of the dead pane
grep -c 'slots saturated' ~/.pi/agent/sessions/--home-notroot--/2026-10-19T19-58-14-875Z_01a155be-4358-7510-bbb6-50d5fb1e8c9b.jsonl

# Mitigation drop-ins and their mtimes
find ~/.config/systemd/user -name '99-hold240.conf' -o -name '98-aimd-floor2.conf' \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort

# Seat-by-seat attribution of the 18:57 roster churn
A=~/.local/state/herdr-seat-manifest/side-projects-pre-stop-2026-10-19T185254-0300.json
B=~/.local/state/herdr-seat-manifest/side-projects-pre-stop-2026-10-19T185753-0300.json
for p in w22:p25 w2J:pH w2K:pP w3E:p3; do
  jq -r --arg p "$p" '.seats[]|select(.pane==$p)|"\(.identity)/\(.provider)/\(.model)"' "$A" "$B"
done
```

Source anchors referenced above (all read-only, no edits made in this slice):

| Fact | Location |
|---|---|
| headerless-429 → budget classification | `src/anthropic_throttle_proxy/proxy.py:3714` (`_budget_under_pressure`), `:3642` (`_plan_lane_has_headroom`), `:3679` (`_plan_lane_siblings_have_headroom`), `:3693` and `:3673` (lane-id guards), `:3770` (caller inversion) |
| AIMD shrink branch + log line | `src/anthropic_throttle_proxy/proxy.py:4013` (`_aimd_apply_pushback`), `:4034` (`is_concurrency_429`) |
| multiplicative decrease + floor | `src/anthropic_throttle_proxy/limiter.py:838` (`shrink`), `:815` (`aimd_floor`), `:857` (floor expression) |
| pre-queue-depth admission reject | `src/anthropic_throttle_proxy/limiter.py:1175` (`_check_queue_admission`), `:1193` (`queue-depth-reject` log) |
| 503 response + log line | `src/anthropic_throttle_proxy/proxy.py:773` (`_queue_wait_timeout_response`), `:813` (`reason=pre-queue-depth`) |
| config defaults | `src/anthropic_throttle_proxy/config.py:151` (AIMD_MIN), `:155` (AIMD_BACKOFF_S), `:167` (CONCURRENCY_COOLDOWN_S), `:190` (`_parse_meter_bindings`), `:260` (AIMD_DECREASE), `:296` (MAX_HOLD_RETRY_AFTER_S), `:325` (QUEUE_MAX_WAIT_S), `:177` (PLAN_METER_LANE) |
| ceiling growth | `src/anthropic_throttle_proxy/proxy.py:4073` (`_aimd_grow`) |

---

## 8. Related work already in this tree (cross-reference, not duplicated)

The 2026-10-19 free-models mission has several parallel slices. This note owns
the **timeline / blast radius / mitigation inventory / residual-risk register**
only; the mechanism analysis and the fixes live elsewhere:

| Artefact | Owner slice | Overlap, and what this note adds |
|---|---|---|
| `specs/334-queue-admission/spec.md` | queue-admission lead (pane `w2P:p1H`) | Same one-sentence root cause and same mechanism. **Gap:** its receipts start at 18:50; §3 here shows the collapse began at **16:47:10** and that earlier 503s landed at 17:01:57 and 17:24:55. |
| `src/anthropic_throttle_proxy/limiter.py:815` (`aimd_floor`, cites *spec 334, 19/10/2026 go-gate incident*) | queue-admission lead | Confirms the floor fix is already merged; §2.3 here documents the pre-fix arithmetic (`int(64 * 0.7) == 44`). |
| `docs/AIMD-POLICY-FREE-LANES.md` | AIMD-policy slice (`limiter.py`) | Same incident framing, AIMD-knob scope only. |
| `docs/free-gate-watchdog.md`, `src/anthropic_throttle_proxy/free_gate_watchdog.py`, `docs/evidence/free-gate-watchdog-2026-10-19/` | observability slice | Targets R8 (503s invisible to `done`-status dashboards). |
| `specs/333-free-gate-spillover/{spec.md,decision-2026-10-19.md}` | spillover slice, lead ruling 19:37 BRT | Independently confirms the shared-bearer finding (R4) and the zen `retry_after_until ≈ +5071 s`. This note adds the **18:27:09 / `retry_after=9171.0`** event — the earliest real upstream 429 found. |
| `specs/opencode-free-tier-limits-2026-10-19.md`, `specs/best-free-model-mimo-vs-longcat-2026-10-19.md` | research slice | Upstream economics; R7's ~2 h penalty is the operational consequence. |
| `docs/FREE-LANE-RUNBOOK.md` | runbook | Should absorb R3 (zai gate missing `99-hold240.conf`) and R6 (240 s hold vs 180 s queue patience). |
