# Free-lane fleet hardening — compaction, schema, and the real concurrency ceiling

- **Date:** terça, 20/10/2026 (verified `date '+%Y-%m-%d %H:%M:%S %Z'` → `2026-10-20 19:03 -03`, `-03` = BRT)
- **Repo:** `~/Documents/Code/yolo-labz/anthropic-throttle-proxy` (branch `342-orchestrator-audit`, GitButler workspace)
- **Scope:** why free-model panes still "have hiccups", the compaction failure proved at its root, the
  `models.json` schema defect, and the measured free-vs-paid ceiling for maximum parallel width.
- **Nothing committed.** No `src/` edits. One NixOS generator edit + one runtime pi extension + tests.

---

## 0. The three defects, in the order they brick a pane

| # | Defect | Class | Status |
|---|---|---|---|
| 1 | **The zen free gate answers `403` to any request with no `tools` array** — and compaction summarization sends exactly that | deterministic, 100 % reproducible | **fixed** (offload extension, tested) |
| 2 | **`models.json` was schema-invalid** (`video`/`audio`/`pdf` modalities); pi rejected the whole file and loaded **0 providers** | deterministic | **fixed at source** (generator) + live file valid |
| 3 | **AIMD collapses the ceiling on rare tail-429s and recovers ~30 s/slot** | statistical | open — tuning recommendation below |

---

## 1. Compaction — the actual root cause

### 1.1 What was reported

Pane `<pane>`, model `opencode/mimo-v2.6-flash-free`:

```
Auto-compaction failed: Summarization failed: 403: {"type":"FreeTierError",
  "message":"OpenCode's free tier can only be used from within OpenCode. ..."}
Model: deepseek-v4.1-flash
```

The prior research note (`3. Resources/🛠️ Tools/Context overflow — 200K free-model panes ... 2026-10-20`)
attributed this to an *intermittent* free-tier lock (26×200 / 26×403) and prescribed
early compaction (reserveTokens 70000) + a lane-independent summarizer. Both are sound, and
the second is what I implemented — but the trigger frequency was misread, which matters
because it changes the fix from "reduce the odds" to "stop sending that request shape".

### 1.2 The real mechanism (10/10 vs 10/10)

The zen gate `:8774` **refuses tool-free inference deterministically**. Same model, same
bearer, same headers, same session id — only the `tools` array differs:

```
zen/mimo-v2.6-flash-free   tools=0 :8774 -> 403   (10 of 10)
zen/mimo-v2.6-flash-free   tools=4 :8774 -> 200   (10 of 10)
```

Full sweep, `:8774`, `authorization: oc_sk_…` + `user-agent: opencode/1.18.31` + `x-opencode-session`:

| model (`:8774` zen) | tools=0 | tools=4 |
|---|---|---|
| `mimo-v2.6-flash-free` | **403** | 200 |
| `longcat-2.5-preview-free` | **403** | 200 |
| `beam-free` | **403** | 200 |
| `deepseek-v4.1-flash-free` | **403** | 200 |
| `glm-5.3-flash-free` | **403** | 200 |
| `nemotron-3.5-lightning-free` | **403** | 200 |
| `cat-jam-free` | **403** | 200 |
| `space-bunny-free` | 410 | 410 (gone) |

Compaction summarization is a `complete()` call with **no tools**, so on every zen free model
it is a guaranteed 403. That is why it looked "intermittent": it correlated with *which call
type* hit the gate, not with time.

The go gate `:8770` is the opposite — it accepts tool-free requests:

| model (`:8770` go) | tools=0 | tools=4 |
|---|---|---|
| `longcat-2.5-preview-free` | **200** | 200 |
| `beam-free` | **200** | 200 |
| `cat-jam-free` | **200** | 200 |
| `step-5-preview-free` | 400 (model unavailable) | 400 |
| `mimo-v2.6-flash-free` | 400 (not on the go catalog) | 400 |

### 1.3 The fix

`~/.pi/agent/extensions/compaction-offload-free.ts` — pi's `session_before_compact` hook,
which returns a `compaction` object and otherwise falls through to pi's default:

- **Route gate (privacy):** offload only when the session provider is `opencode` or
  `opencode-go`. Both are the same Chinese-frontier vendor family, so the summary adds no new
  exposure. `zai` keeps the fleet's existing offload path; `openai-codex`/`anthropic` (every
  privacy-pinned seat) are never touched and keep default compaction.
- **Candidates, in order:** `opencode-go/longcat-2.5-preview-free` (1 M ctx), then
  `opencode-go/beam-free`. A saturated lane is walked past, not fatal.
- **Brief contract:** the summary must contain `## task / ## state / ## next action /
  ## evidence / ## blockers`. A missing section is a **failed** summary and is discarded —
  a brief that reads well but lost the next action only surfaces as a seat resuming in the
  wrong direction.
- **Fallback:** any error, absent model, empty or invalid brief → `undefined` → pi's default
  compaction. Worst case is the status quo ante, never worse.

### 1.4 Executable acceptance

`extensions/test-compaction-offload-free.mjs` — 15 assertions (route gate, candidate class,
brief validation discriminating). `extensions/test-compaction-offload-free.handler.mjs` —
10 assertions driving the **real** registered handler with a fake `ExtensionAPI`/`ctx`:

```
== A. happy path: first candidate produces a valid brief ==        PASS
== B. first lane fails -> walks to the next, still compacts ====== PASS
== C. every lane fails -> undefined (never worse) ================= PASS (x2)
== D. unusable summaries are rejected, not shipped =============== PASS (x2)
== E. model absent from this host is skipped, not fatal =========== PASS
== F. privacy gate: zai / openai-codex / anthropic untouched ====== PASS (x3)
== G. abort respected ============================================ PASS
15 passed, 0 failed  (unit)   10 passed, 0 failed  (handler)
```

Live lane, exact summarization request shape (tool-free, 0-tool → 200 on the go gate):

```
POST :8770 longcat-2.5-preview-free  HTTP 200
summary chars: 2030
sections present: {'## task': True, '## state': True, '## next action': True,
                   '## evidence': True, '## blockers': True}
```

End-to-end, a real pi turn on the fixed stack:

```
pi -p --no-session --model opencode/mimo-v2.6-flash-free "Reply with exactly: ZEN_OK"
→ ZEN_OK
pi -p --no-session --model opencode/longcat-2.5-preview-free "Reply with exactly: PANE_OK"
→ PANE_OK
```

`pi config` lists `compaction-offload-free.ts` as enabled.

---

## 2. `models.json` — the boot error every pane printed

`pi --list-models` and every fresh seat printed:

```
Invalid models.json schema:
  - providers.opencode.models.22.input.2: must match a schema in anyOf
  ...
providers loaded: (NONE)
```

**Cause.** pi's schema (`dist/core/model-config.js:175,190` of the pinned 1.1.0 build) accepts
exactly two input modalities:

```js
input: Type.Optional(Type.Array(Type.Union([Type.Literal("text"), Type.Literal("image")])))
```

models.dev publishes a wider vocabulary (`video`, `audio`, `pdf`). The generator copied it
verbatim, so **37 of 113** emitted models carried a rejected modality and pi discarded the
**entire file** — all 14 providers, including both opencode gates' `baseUrl`/`apiKey`.

**Fixed at the source, not the artifact.** `NixOS/user-scripts/gen-pi-opencode-models.py` now
filters through `PI_INPUT_MODALITIES = ("text", "image")`, with the reason recorded inline.
The generated `.nix` is patched to match (113 `input` lines; diff is `input` only).

Live verification of the currently-linked file:

```
ModelConfig.load(~/.pi/agent/models.json) → ERROR: (NONE)
providers: 14 → codex-a, codex-c, deepinfra, deepseek, mimo-desktop,
             mimo-desktop-subscription, openai-codex, opencode, opencode-go,
             xiaomi, xiaomi-token-plan-ams, xiaomi-token-plan-cn,
             xiaomi-token-plan-sgp, zai
opencode models: 84     opencode-go models: 29
modalities in live file: text, image     models still schema-invalid: 0
```

The live `~/.pi/agent/models.json` was repointed to a clean copy at 19:42 today by a sibling
seat; that copy and my generator fix agree (text/image only). The NixOS change is what makes
it survive the next rebuild.

---

## 3. Maximum parallel width without crossing — measured

### 3.1 The user's hypothesis is correct: paid and free are different regimes

All counts from `journalctl --user -u opencode-go-throttle-proxy --since 2026-10-19`, parsed per
`done … status=N … model=…`:

| model | total | 200 | 429 | 499 | **429 rate** |
|---|---:|---:|---:|---:|---:|
| `deepseek-v4.1-flash` (paid) | 1763 | 1635 | **1** | 126 | **0.06 %** |
| `longcat-2.5-preview-free` (free) | 892 | 864 | **10** | 16 | **1.12 %** |
| `glm-5.3` | 13 | 13 | 0 | 0 | 0 % |
| `beam-free` | 5 | 3 | 0 | 0 | 0 % |

**Free lane 429s ~18× more often per request than the paid lane.** Same gate, same bearer,
same limiter — the difference is the upstream tier.

### 3.2 Concurrency is NOT the trigger — long generations are

Every one of the 10 free-model 429s landed at the **98.6th percentile or above** of the
successful-duration distribution:

```
longcat-free successes: p50=3573ms  p90=7866ms  p99=45243ms  max=75363ms
429 durations         : p50=36626ms  min=35755ms  max=50093ms
  429 at 35755ms = 98.6th pct of successful durations
  429 at 50093ms = 99.5th pct
```

And the inflight count at each 429 was **low**, not high: `inflight=0, 0, 0, 0, 0, 0, 14, 18,
25, 16`. Meanwhile **247 requests returned 200 while inflight was 63**.

Direct ramp against the live gate (8-token replies; one process per request budget on the
first attempt hit the job cgroup's 64-task cap, so the measurement uses threads over urllib):

```
:8770 longcat-2.5-preview-free
   N    200   429   503   5xx  other    p50s    maxs  verdict
   4      4     0     0     0      0     6.4     6.4  OK
   8      8     0     0     0      0     4.9     5.2  OK
  12     12     0     0     0      0     2.5     5.5  OK
  16     16     0     0     0      0     3.3     5.3  OK
  20     20     0     0     0      0     6.3     6.8  OK
  24     24     0     0     0      0     4.7     6.2  OK
  32     32     0     0     0      0    31.7    34.1  OK
  40     40     0     0     0      0    21.2    22.3  OK
  48     48     0     0     0      0     7.4    11.2  OK
  64     64     0     0     0      0     4.6     7.1  OK
  96     96     0     0     0      0     3.8     7.4  OK
 128    128     0     0     0      0     7.4    16.6  OK
```

**Zero 429s at 128 concurrent.** The throughput soft limit is upstream serialisation, visible
as the latency wall at N=32–40 (±30 s), not refusal. So raising `max_concurrent` is not what
buys width — and never was: the ceiling sat at **1024 all day** while observed inflight peaked
in the sixties.

### 3.3 What actually destroys width: the AIMD asymmetry

`limiter.py:~895` (shrink) vs `~1179` (grow), `config.py:260` `AIMD_DECREASE=0.7`,
`AIMD_BACKOFF_S=30`:

- **shrink:** `new = max(floor, min(int(max * 0.7), max - 1))` — multiplicative.
- **grow:** `+1` per success, gated by a 30 s cooldown.

```
ceiling 1024 -> 20 takes 12 sporadic 429s
ceiling  256 -> 20 takes  8 sporadic 429s
ceiling   64 -> 20 takes  4 sporadic 429s

recovery 20 -> 64  :  44 grows x 30s =  22.0 min of clean traffic
recovery 20 -> 128 : 108 grows x 30s =  54.0 min
recovery 20 -> 1024: 1004 grows x 30s = 502.0 min
```

A **1.12 %** 429 rate on the free lane is therefore not a cosmetic blip: it is a ratchet.
Four tail-timeouts cost 22 minutes of width. This — not the semaphore size — is the "crossing
the line" the fleet feels.

Live while this was written, the go gate moved **20 → 28 → 43** and `hard_max` went
**1024 → 64** (from `/__throttle/health`, not from the unit's environment — so a sibling seat
applied it through the runtime config setter). Current state, `bearer e574f38a`:

```
:8770  max_concurrent=43  hard_max=64  inflight=11  recent_shrinks=0  successes_since_throttle=9
:8774  max_concurrent=15  hard_max=64  inflight=0   recent_shrinks=0  successes_since_throttle=5
unit env: THROTTLE_AIMD_INITIAL_CONCURRENT=12  THROTTLE_AIMD_MIN=2   (floor still 2)
```

So the hard max already matches the recommendation below. The **remaining** gaps are the floor
(2) and the backoff (30 s) — the two knobs that decide how badly one tail-429 hurts.

### 3.4 Recommendation for a free-lane profile

| knob | now | free-lane target | why |
|---|---|---|---|
| `THROTTLE_AIMD_INITIAL_CONCURRENT` | 12 | **24** | measured clean at 24; well inside the 128 proven |
| `THROTTLE_AIMD_MIN` | 2 | **8** | a floor of 2 turns one tail-429 into a collapse; 8 keeps the lane usable |
| hard max | 1024 | **64** | never binds throughput (inflight peaks ~60) but bounds the ratchet: 4 bad events → 20, not 12 |
| `THROTTLE_AIMD_BACKOFF_S` | 30 | **10** | recovery dominates the cost: 20→64 becomes 7 min instead of 22 |
| `hard max` | **64 (applied live)** | 64 | already done; bounds the ratchet to 4 events → 20 |
| contract `queue_max_wait` | 180 s | keep | verdict at 8 concurrent × 300 s generation still fits 180 s |
| compaction reserve | 16384 | **per-model 70000** | already applied by a sibling for the four free ids; keep |

Expected width at these settings: **~24–60 concurrent free generations sustained**, versus the
observed 20 → 2 collapse loop. The binding constraint is upstream serialisation latency, not
a number this repo can raise.

---

## 4. Fleet state (receipts)

`herdr --session <s> api snapshot` across the 6 running sessions → **253 panes**; model read
from the last assistant message of each pane's session JSONL (statusline is a fallback and is
truncated in the footer).

| panes | work | verdict |
|---:|---|---|
| 167 | no session file | `now:` configuration only — cannot be measured, not a claim |
| 25 | `zai/glm-5.3` | paid, intentional (review/privacy tiers) |
| 18 | `opencode/mimo-v2.6-flash-free` | **free, correctly routed to `:8774`** |
| 2 | `opencode-go/longcat-2.5-preview-free` | free, correctly routed to `:8770` |
| 22 | `opencode-go/mimo-v2.6-flash` | **NON-FREE** |
| 15 | `opencode-go/deepseek-v4.1-flash` | **NON-FREE** |
| 3 | `zai/glm-5.3-flash` | paid |
| 1 | `openai-codex/gpt-6.1-sol` | non-free, no local gate |

**37 panes on a non-free opencode model** (35 idle/done, 2 `working`). Routing is otherwise
clean: every free-catalog model resolves to `:8770`/`:8774`; zero free-model panes misrouted.

### 4.1 Migration — mechanism verified, execution blocked by fleet law

The supported verbs, all tested live on `<pane>`:

1. **`/new-model <provider>/<model>`** — refused by the pane itself:
   *"Same-family replay fits the target window; use /model instead of creating a replacement
   session"*. Correct for a cross-family switch, not for free→free.
2. **`/model <provider>/<model>`** — opens pi's model picker, pre-filled with the exact target
   (`> opencode/mimo-v2.6-flash-free:high`), awaiting `Enter to select`. `herdr pane send-keys
   … Enter` **and** `ctrl+m` did not commit it; `Escape` cleared the picker cleanly. So this
   path needs an operator's keypress or a focused attach — it is not scriptable from a peer seat.
3. **The durable path** is not UI at all: a seat's model comes from the wrapper's
   `agentModelsTable` (`name → model`) at launch. 37 panes are on non-free models that do not
   appear in that table at all, so their model came from runtime `/model` changes recorded in
   each session's `model_change` events — i.e. they were hand-migrated earlier and are now
   stale. Fixing them for good means either restarting each seat after the table is updated, or
   one `/model` per pane.

**Cross-workspace block (by design).** Every attempt to drive another workspace was refused by
`fleet-rules` C2, including README-level `--help` on `herdr pane`, because the guard cannot
prove a loop variable's target is local:

```
fleet-rules C2: targets outside workspace w2P or outside session: session:study.
Cross-workspace/cross-session writes are reserved to the operator and the fleet-lead during a
declared incident — hand off to that workspace's head instead (herdr-prompt).
```

I did **not** set `HERDR_DESTRUCT_CONFIRM`/`HERDR_CROSS_WS_OK` to force it: one seat pushing a
modal keystroke sequence into 37 panes in 5 other workspaces is exactly the failure the law
exists to prevent. The worklist stays with the lead.

`<pane>` was left exactly as found (picker open → `Escape`, model unchanged).

---

## 6. Open items

1. **`THROTTLE_FREE_BUDGETS` is unset**, so `free_budget.py` is inert — the declared-allowance
   path has never gated anything on either free gate. If the fleet wants a hard "never cross"
   guarantee rather than AIMD reaction, that knob is the one to set (§3.4 is the reactive
   alternative).
2. **`space-bunny-free` returns 410** on the zen gate — dead id still in the catalog.
3. **`step-5-preview-free` returns 400 "Model is unavailable"** on the go gate.
4. **167 panes have no session file** and cannot be audited at all; they boot into `now:` and
   never run. Whatever model they are configured with is a claim, not a measurement.
5. **`.worktrees/` debt** (44 orphan dirs, 92 MB) and the 139-branch backlog from the 19/10 audit
   are untouched — separate slice, still needs a human.
