---
title: >-
  Best free model for fleet agentic coding — MiMo-V2.6-Flash Free vs
  LongCat-2.5 Preview Free
id: best-free-model-mimo-vs-longcat-20261019
created: 2026-10-19
status: research-complete
scope: FREE opencode models only (zen + go gates)
gates:
  go: :8770 → https://opencode.ai/inference/go (bearer e574f38a)
  zen: :8774 → https://opencode.ai/inference (bearer e574f38a)
---

# Best free model — MiMo-V2.6-Flash Free vs LongCat-2.5 Preview Free

**Slice:** mimo-vs-longcat research. **Date:** 19/10/2026 (BRT).
**Question:** which *free* opencode model should carry this fleet's agentic
coding work at maximum parallel width without ever crossing the rate limit?

## Recommendation

**Primary: `mimo-v2.6-flash-free` (zen gate :8774), ceiling ≈ 8 concurrent.**

**Fallback: `longcat-2.5-preview-free` — but on a DEDICATED bearer, never on a
bearer shared with paid traffic, and with its own low ceiling (≤ 4–6).**

The deciding factor is *not* raw capability. Both are multimodal reasoning
models at $0 and both are genuinely usable for agentic coding. The deciding
factor is **the shape of each model's rate-limit cliff and who else it takes
down when it falls off**. MiMo-free's cliff is a rare but hard 2-hour
gate-wide dark window you can stand well clear of. LongCat-free's cliff is a
cheap per-request pushback that *recursively collapses the shared AIMD ceiling
to 1*, which starves the **paid** traffic riding the same bearer — that is
exactly the 19/10 18:57–19:06 incident.

> One genuine LongCat advantage is held against it here: its **free tier keeps
> the full 1M / 131K window**, whereas MiMo's free tier is cut to **200K / 32K**
> (§4). The paid `mimo-v2.6-flash` is 1M-class, the *free* one is not. For the
> longest agentic sessions that overflow 200K, that alone can outweigh MiMo's
> benchmark edge — see §8 Q3.

---

## 1. Verifiable benchmark evidence

### MiMo-V2.6 (Xiaomi) — published, open-sourced, RL-scaled

Read directly from Xiaomi's own release page [X1]. Each figure below is tagged
with **exactly which model it describes** — three different V2.6-family
artifacts are quoted in secondary coverage and conflated constantly.

**MiMo-V2.6-Flash (the free model under evaluation):**

| Claim | Score / fact | Source |
|---|---|---|
| **DeepSWE v1.1** (long-horizon software engineering, out-of-sample) | **48.8 → 65.7** (+~17 pts after RL) | [X1] |
| Average pass rate on RL training tasks | relatively **+25%** | [X1] |
| RL budget | 30 steps, ~750K cumulative trajectories, ~**US$850K** cost, 6 days live RL | [X1] |
| Positioning vs prior gen | **"comprehensively outperformed MiMo-V2.5-Pro"** *(qualitative — Xiaomi publishes no SWE-bench number for V2.6-Flash itself)* | [X1] |

**MiMo-V2.6-Pro (paid — context only, NOT the free model):**

| Claim | Score / fact | Source |
|---|---|---|
| DeepSWE v1.1 | 58.4 → **72.6** (+~14 pts) | [X1] |
| Artificial Analysis Intelligence Index | **46** — "the most powerful open-source model available", passing Kimi K3 and Qwen3.8 Max | [X1] |
| Agent benchmarks | "on par with Claude Opus 5 and GPT-5.6 Sol" on most | [X1] |
| RL cost | ~**US$2.62M**, 6 days live RL | [X1] |

**MiMo-V2.6-Distill-Qwen-9B (the 9B distill used as the RL *starting point*):**

| Benchmark | Before → after RL | Source |
|---|---|---|
| SWE-bench Verified | **61.1 → 66.2** | [X1] |
| Terminal Bench 2.1 | **37.1 → 52.8** | [X1] |
| MiMo Cyber Bench | 31.3 → 47.0 | [X1] |
| MiMo Visual Coding | 64.0 → 72.4 | [X1] |

> ⚠️ **Two provenance traps (confidence: high, verified by reading [X1]
> directly).** Secondary coverage — including Google's AI Overview, which is
> where the 61.1→66.2 figure is most often seen — attributes **SWE-bench
> Verified 66.2** to *MiMo-V2.6-Flash*. **It is not.** Xiaomi's page states it
> under "Starting RL training from **MiMo-V2.6-Distill-Qwen-9B**" [X1]. Likewise
> the widely-quoted **73.4%** SWE-bench figure belongs to the *MiMo-V2-Flash*
> technical report [A1] — an **earlier, different V2-series model (Jan 2026)**,
> not V2.6-Flash. Neither number is a V2.6-Flash number.
>
> **The honest V2.6-Flash-specific external evidence is DeepSWE v1.1 = 65.7**
> [X1]. That is still a published, agentic-coding-specific, out-of-sample
> benchmark — and it is measured on the model actually under evaluation.

**Architecture lineage (from the earlier V2 report, context only [A1]):**
MiMo-V2-Flash is MoE 309B total / 15B active, 27T pretraining tokens,
SWE-Bench Verified 73.4% and Multilingual 71.7%, with a 3-layer MTP module
(0.33B) giving 3.6 acceptance length and a 2.6× decoding speedup. Xiaomi does
not state V2.6-Flash's parameter count on [X1], so **do not assume V2 and V2.6
Flash share an architecture** — cite [A1] only as family lineage.

### LongCat-2.5-Preview (Meituan) — no published benchmarks at all

| Claim | Fact | Source |
|---|---|---|
| Official eval table / model card | **None published** | [B1][B2] |
| Weights | **Not released** (API + partner platforms only), unlike open-weight LongCat-2.0 | [B1][B2] |
| Architecture | MoE ~1.6T total / ~48B active | [B1] |
| Context / output | 1M native / 128K max | [B1] |
| Release | ~26/09/2026 | [B1] |
| OpenCode's own capability panel | Coding **50/100 — "No data; neutral placeholder"**; Reasoning 50/100 — "no comparable benchmark"; Tool use 50/100 — "no comparable benchmark" | [OC4] |

`opencode.ai/data/compare` renders LongCat's coding/tool-use/reasoning cells as
neutral placeholders because there is nothing to score them against [OC4]. That
is the single clearest external statement of the maturity gap: **we cannot
externally verify LongCat-2.5-Preview's agentic coding ability at all.**

---

## 2. OpenCode's own real-world usage + retention data

OpenCode publishes live telemetry from its own client fleet [OC5] [OC6] [OC7].
For "which free model actually holds up under agentic coding load", retention is
the most decision-relevant number — it measures users who tried it and *kept*
using it.

| Metric | MiMo-V2.6-Flash | LongCat-2.5-Preview |
|---|---|---|
| Usage rank (last week) | **#04** | #08 |
| Tokens, last 2 months | **42T** | 8.4T |
| Unique users | **818K** | 252K |
| Completed sessions | **31,226,651** | 14,418,331 |
| Token share | **4.0%** | 0.8% |
| **Weekly retention** | **81.9%** | **63.8%** |
| Leaderboard retention rank | **#01 of the entire leaderboard** (4.6K user-weeks) | **absent from the top 15** |

Sources: [OC5] (mimo page), [OC6] (longcat page), [OC7] (leaderboard, updated
19/10/2026 18:55 GMT-3). On the retention leaderboard `longcat-2.0` — the *older,
open-weight* predecessor — sits at #14 with 66.5%; `longcat-2.5-preview` does not
place [OC7].

### Normalized capability panel [OC4]

| Axis | LongCat-2.5-Preview | MiMo-V2.6-Flash |
|---|---|---|
| Reasoning | 50 *(no comparable benchmark)* | 31 |
| Coding | 50 *(no data — placeholder)* | **58** |
| Tool use | 50 *(no comparable benchmark)* | **55** |
| Cost efficiency | 58 | **77** |
| Context window | 81 | **92** |
| Multimodal | 33 | **67** |

Note the panel scores the *base* models (paid list prices), not the free tiers;
the free tiers are the same checkpoints with reduced context caps (§4).

---

## 3. Local fleet evidence (ground truth from this host's gates)

Both are live behind throttle proxies that log per-model, per-status lines with
elapsed_ms, and expose Prometheus `/metrics`. Sampled 19/10/2026 ~19:19–19:40 BRT
from the proxy's retained journal (units `opencode-zen-throttle-proxy` :8774 and
`opencode-go-throttle-proxy` :8770).

### Zen gate :8774 — where mimo-free actually runs

Completed requests by model × status (`done` lines):

| Model | 200 | 429 | 403 | 499 | other |
|---|---|---|---|---|---|
| **mimo-v2.6-flash-free** | **757** | **3** | 13 | 112 | — |
| longcat-2.5-preview-free | 3 | 0 | 0 | 0 | — |
| nemotron-3-ultra-free | 3 | 0 | 0 | 1 | — |
| nemotron-3.5-super-vl-free | 2 | 0 | 0 | 0 | — |
| glm-5.3-flash-free | 0 | 1 | 2 | 0 | — |
| deepseek-v4.1-flash-free | 0 | 0 | 1 | 0 | — |
| step-5-preview-free | 0 | 0 | 0 | 0 | 5× 400 |
| beam-free | 0 | 0 | 0 | 0 | 1× 400 |
| space-bunny-free | 0 | 0 | 0 | 0 | 1× 410 |
| cat-jam-free | 0 | 0 | 0 | 0 | 1× 400 |

Latency per model (from `elapsed_ms` on completed requests):

| Model | n | p50 | p90 | max | avg |
|---|---|---|---|---|---|
| **mimo-v2.6-flash-free** | 997 | 13.8s | 43.5s | 173.7s | 20.5s |
| longcat-2.5-preview-free | 3 | 5.5s | 13.4s | 112.2s | 43.7s |
| nemotron-3-ultra-free | 5 | 6.0s | 6.9s | 57.0s | 15.9s |
| step-5-preview-free | 5 | 0.7s | 0.7s | 0.8s | 0.7s |

**mimo-free is the zen lane's proven workhorse** — 757 successful agentic
completions vs 3 for every other free model combined. The 112 `499`s are client
disconnects (11.2%), i.e. slow generations abandoned by clients, not failures.

**The zen 429s are REAL rate limits with a ~2-hour Retry-After:**

```
Oct 19 18:27:09 rate-pushback-retry bid=e574f38a status=429 retry=1/1
  pause=9171.0 retry_after=9171.0 synthetic_pause=False
Oct 19 18:57:20 rate-pushback-retry bid=e574f38a status=429 retry=1/1
  pause=7361.0 retry_after=7361.0 synthetic_pause=False
  (model=mimo-v2.6-flash-free, done line inflight=14)
```

`9171s ≈ 2h33m`, `7361s ≈ 2h03m`. `/__throttle/health` on :8774 at 19:34:33
confirms the gate is **still dark**:

```json
"last_ratelimit": {"retry-after": "7361"},
"retry_after_until": <19/10/2026 21:00:01 BRT>,
"retry_probe_required": true, "retry_probe_blocks_routing": true,
"limiter": {"max_concurrent": 8, "hard_max": 1024, ...}
```

So the zen free tier for mimo-free went dark at 18:57 and stays dark until
**21:00 BRT** — ~2h. Both wall events fired at inflight ≈ 12–14 on a gate whose
`THROTTLE_AIMD_INITIAL_CONCURRENT` is 12.

### Go gate :8770 — where longcat-free ran

| Model | 200 | 429 | 499 | notes |
|---|---|---|---|---|
| **longcat-2.5-preview-free** | 584 | **18** | 81 | the only model producing 429s on this gate |
| **mimo-v2.6-flash** *(PAID, same bearer)* | 1030 | **0** | 23 | never rate-limited |

Latency / throughput:

| Model | n | p50 | p90 | p99 | max | avg | output tok/s |
|---|---|---|---|---|---|---|---|
| longcat-2.5-preview-free | 764 | **7.4s** | 36.5s | 124.8s | 290.6s | 14.2s | **48.7** |
| mimo-v2.6-flash (paid) | 1053 | 32.6s | 96.8s | 311.3s | 951.8s | 50.1s | 30.8 |

(Throughput from `/metrics`: longcat 282,613 output tok over 5,802.7s across 461
requests; mimo-paid 146,579 tok over 4,752.6s across 180. Both are **below
Pedro's ≥100 tok/s gate** recorded in `NixOS/meta/decisions.md` ~1435.)

**LongCat-free is genuinely fast** — p50 7.4s and the highest output throughput
on the go gate. This is its real strength.

### The incident mechanism — longcat's cliff has collateral damage

All **61** `aimd-shrink` events on the go gate are budget pushback:

```
aimd-shrink bid=e574f38a status=429 max_concurrent=1 retry_after=0.0
  pause=30.0 synthetic_pause=True (budget/real-retry-after pushback)
```

Because the go upstream sends **no `Retry-After` header** (`retry_after=0.0`,
`synthetic_pause=True`), the proxy synthesises a 30s pause per pushback. Under
sustained budget pressure that produced **1 shrink per minute, continuously,
18:21 → 18:57**, driving the shared bearer's ceiling through
`44 → 30 → 23 → 17 → 16 → 14 → 12 → 11 → 9 → 8 → 4 → 3 → 2 → 1` (16 of the 61
shinks landed on `max_concurrent=1`).

At ceiling 1 with 171–303s generations, every other request failed pre-queue:

```
queue-depth-reject bid=e574f38a slots=1 busy=1 queued=0 ahead=0
  service_s=201.748 source=inflight est_wait_s=201.748 max_wait_s=179.981 max_depth=0
queue-wait-timeout bid=e574f38a reason=pre-queue-depth max_wait_s=179.981
  inflight=1 max_concurrent=1 service_s=201.748 est_wait_s=201.748 retry_after_s=202
```

**16** `queue-wait-timeout` 503s total — 12 × `pre-queue-depth`, 4 × `elapsed` —
clustered at 17:57:24–17:57:39 (4) and **19:02:24–19:03:03 (10)**, matching the
documented incident window.

> **The key local finding (confidence: high).** On the go gate, **paid
> `mimo-v2.6-flash` produced zero 429s while `longcat-2.5-preview-free` produced
> all 18**, on the *same bearer* `e574f38a`. LongCat's free-tier budget pushback
> collapsed a **shared** AIMD ceiling, and the resulting 503 storm hit the paid
> lane and killed pi panes in retry loops. LongCat-free's failure mode is not
> self-contained.

### Gate configuration as measured

```
go  (opencode-go-throttle-proxy):  CLAUDE_API_THROTTLE_MAX=1024
  THROTTLE_AIMD_INITIAL_CONCURRENT=12  THROTTLE_AIMD_MIN=2
  THROTTLE_QUEUE_MAX_WAIT_S=180        THROTTLE_MAX_HOLD_RETRY_AFTER_S=240
zen (opencode-zen-throttle-proxy): CLAUDE_API_THROTTLE_MAX=1024
  THROTTLE_AIMD_INITIAL_CONCURRENT=12
  THROTTLE_QUEUE_MAX_WAIT_S=180        THROTTLE_MAX_HOLD_RETRY_AFTER_S=240
```

Client contract alignment: pi idle timeout 300s > hold 240s > queue_max_wait
180s, so a request can never sit queued longer than the client's patience. Current
live AIMD: go `e574f38a = 36` (recovered after the restart that re-registered the
bearer at 12), zen `e574f38a = 8` (clamped while the 2h hold is active).

---

## 4. Free-tier constraints, quota and pricing

### Catalog ground truth (models.dev, the same source the opencode client reads)

`modules/home/lib/pi-opencode-models.nix` is generated verbatim from
`https://models.dev/api.json` by `user-scripts/gen-pi-opencode-models.py`
(providers `opencode` = zen, `opencode-go` = go). Free tier is `cost = 0`.

| | `mimo-v2.6-flash-free` | `longcat-2.5-preview-free` |
|---|---|---|
| Reasoning | true | true |
| Input modalities | **text, image, audio, video** | text, image |
| Context window (free tier) | **200,000** | **1,000,000** |
| Max output tokens (free tier) | **32,000** | **131,072** |
| Cost | 0 / 0 / 0 | 0 / 0 / 0 |
| Gates present on | **zen only** (line 931) | **both** zen (918) **and** go (1345) |

> **Free-tier context caps differ sharply from the paid models.** The paid
> `mimo-v2.6-flash` is 1,048,576 ctx / 131,072 out; the *free* variant is
> **200,000 ctx / 32,000 out** — a 5× context and 4× output reduction.
> `longcat-2.5-preview-free` keeps the full 1M / 131K. For very long agentic
> sessions (large repos, many tool rounds) LongCat-free's 1M window is a real,
> concrete advantage that must be weighed against everything above.

### Pricing (paid reference, to price the non-free fallback)

| | input /Mtok | output /Mtok | cached input |
|---|---|---|---|
| mimo-v2.6-flash | **$0.14** | **$0.28** | $0.0028 |
| longcat-2.5-preview | $0.30 | $1.20 | $0.006 |

MiMo is **2.1× cheaper on input and 4.3× cheaper on output** [OC4][OC3].

### Quota / availability mechanics

- **Go** = subscription, **$10/month** (Go) or **$40/month** (Go Plus), single-
  member workspaces only [OC2]. Its model list includes
  *"LongCat 2.5 Preview Free (limited time)"* but **only paid
  `MiMo-V2.6-Flash` / `MiMo-V2.6-Pro` — mimo-free is not on Go** [OC2].
  LongCat-free, Beam-free and MiniMax-M3.1-Flash-free are all tagged
  **"limited time"** [OC2] — i.e. they can be withdrawn.
- **Zen** = pay-as-you-go credits, and the free models there are listed with
  **"Free / Free / Free"** for input/output/cached input [OC3].
- **Free-tier client lock (operational hazard).** Zen free rejects any client it
  does not recognise. 13 of mimo-free's requests on zen returned:

  ```
  credential-reject status=403 detail="OpenCode's free tier can only be used
  from within OpenCode. Download it at https://opencode.ai and keep building."
  ```

  escalating to `credential-dead reason=streak-3`. **Any seat driving these free
  models must present as a recognised opencode client** (verified clients per
  [OC2]: Hermes, Claude Code, Codex, ZCode, **Pi**, jcode, Kilo Code). Non-opencode
  clients get 403, and three in a row kills the credential for the whole gate.
- **Free lane rotation.** The zen free catalog rotates: Space Bunny free was
  retired 06/10 after a ~2-week promo; Step-5-Preview-Free ran ~10/10→17/10;
  longcat/cat-jam/ling-3.1-flash/exo-free/oxa-free rotate in and out
  (vault: `3. Resources/🛠️ Tools/opencode-zen-offers-2026-10.md`, Reddit sweep
  15/10/2026 via `reddit-phsb5321`). Consistent with [OC2]'s "limited time"
  tags. **Do not build a lane whose loss would be operationally painful.**

---

## 5. Maturity

| | MiMo-V2.6 | LongCat-2.5-Preview |
|---|---|---|
| Status | stable series release (V2 → V2.5 → V2.6 lineage) | **preview** |
| Weights | **open-sourced** (V2.6 series announced as fully open-source) | **not released** |
| Tech report | published + open-sourced | **none** |
| Training env + RL code | **open-sourced** | none |
| Benchmarks published | SWE-bench, DeepSWE, Visual Coding, AAII | **none** |
| Multimodal | text / image / **audio / video** | text / image |
| Upstream org | Xiaomi | Meituan |
| "Limited time" tag on opencode | not tagged (permanent catalog) | **tagged limited time** on Go [OC2] |
| Weekly retention | **81.9%** (#1 on leaderboard) | 63.8% (unplaced) |

Confidence: high on MiMo's openness (Xiaomi's own release note [X1]); high on
LongCat's opacity (no evals, no weights, confirmed by two independent trackers
[B1][B2] and by OpenCode's own placeholder cells [OC4]).

---

## 6. Recommendation — reasoning

### Why MiMo-V2.6-Flash Free wins

1. **It is the only one of the two whose agentic coding ability is externally
   verifiable at all.** MiMo-V2.6-Flash publishes **DeepSWE v1.1 = 48.8 → 65.7**
   on the model itself [X1], and the surrounding V2.6 family publishes further
   agentic scores (Pro: DeepSWE 72.6, AAII 46; Distill-Qwen-9B: SWE-bench
   Verified 66.2, Terminal Bench 2.1 52.8) [X1]. **LongCat has no benchmark,
   no model card and no weights** [B1][B2], and OpenCode's own comparison panel
   renders its coding/tool-use/reasoning cells as "no data" placeholders [OC4].
   *(Precision note: the commonly quoted SWE-bench 66.2 and 73.4% figures are
   **not** V2.6-Flash's — see §1. The V2.6-Flash-specific number is DeepSWE
   65.7. The point stands either way: one model is measured, the other is not.)*
2. **#1 retention on the entire OpenCode leaderboard** — 81.9% weekly, 818K
   users, 31.2M completed sessions [OC5][OC7]. LongCat-2.5-Preview is unplaced
   [OC7]. Retention is the best available proxy for "it holds up in real agentic
   loops".
3. **It is the fleet's proven workhorse**: 757 successful zen completions vs 3
   for everything else (§3).
4. **Cheaper fallback path**: 2.1×/4.3× cheaper paid [OC4], and a permanent
   catalog entry rather than a "limited time" one [OC2].
5. **Broader modality** (audio + video) and, on the *paid* tier, 1M context —
   though note the *free* tier is capped at 200K (§4).

### Why LongCat-free is the fallback, not the primary

- No verifiable capability evidence [B1][B2][OC4].
- Its 429s on the go gate are cheap individually but **collapse the shared
  bearer ceiling to 1** and take paid traffic down with it — 12 of the 16
  incident 503s were `pre-queue-depth` at `max_concurrent=1` (§3).
- Tagged **"limited time"** [OC2] and part of a rotating free lane.
- **Its real strengths, stated fairly:** fastest p50 on the fleet (7.4s),
  highest output throughput measured (48.7 tok/s), and the only candidate with
  the full **1M / 131K free-tier window** (§4) — genuinely valuable for the
  longest agentic sessions that would overflow MiMo-free's 200K cap.

### Operating envelope for maximum width without crossing the line

| | Recommended max_concurrent | Rationale (receipts) |
|---|---|---|
| `mimo-v2.6-flash-free` (zen) | **≤ 8** | Both zen wall events fired at inflight 12–14 on a gate whose `THROTTLE_AIMD_INITIAL_CONCURRENT=12`; zen's own limiter now sits at 8 while clamped. Standing at ≤8 keeps real headroom under the 2h-`Retry-After` cliff. (§3) |
| `longcat-2.5-preview-free` (go) | **≤ 4–6, on a dedicated bearer** | 429s sustained 1/min 18:21→18:57 and drove the shared ceiling to 1 with 16 collapses. Keep it off the paid bearer entirely. (§3) |

Two cliff shapes, so two different rules:

- **Zen/mimo-free cliff = rare but hard.** 2 discrete wall events (18:27,
  18:57), each followed by a **~2h gate-wide dark window** that also blocks
  `retry_probe_blocks_routing`. Rare (3/875 ≈ 0.3%) but catastrophic and long.
  **Rule: leave large headroom; when it fires, it is a 2h outage, so switch to
  the fallback rather than waiting.**
- **Go/longcat-free cliff = frequent but shallow, with collateral.** No
  `Retry-After`, synthetic 30s pauses, but **sustained** pressure that
  recursively shrinks a shared ceiling. **Rule: never share its bearer with paid
  traffic; cap it low and independently.**

> **Live note at time of writing (19/10/2026 19:34 BRT):** the zen gate is
> **currently dark for `mimo-v2.6-flash-free` until 21:00 BRT** (`retry-after:
> 7361`, `retry_after_until` 21:00:01). Until then the fallback applies — and if
> longcat-free must carry load in this window, it must do so on its own bearer.

---

## 7. Fallback ladder (FREE models only)

1. **`mimo-v2.6-flash-free` (zen)** — primary. Verified capability, #1
   retention, fleet-proven. Cap ≤ 8.
2. **`longcat-2.5-preview-free` (dedicated bearer)** — fallback when zen is held
   (as now) or when a session needs >200K context. Cap ≤ 4–6, own bearer,
   accept no benchmark evidence.
3. **`deepseek-v4.1-flash-free`** — third choice if both above are unavailable.
   Highest raw usage on OpenCode overall (88T tokens / 7d, 317K eligible users)
   and 68.5% retention [OC7]; essentially tied with LongCat-2.0. **Caveat
   (confidence: medium):** only 1 request observed locally and it returned
   `403` (client-lock), so this fleet has no meaningful local success data —
   validate before relying on it.
4. **`glm-5.3-flash-free`** — last resort; 2× 403 + 1× 429 in 3 local requests.
   Also note the fleet's own recorded judgement that `glm-5.3-flash` fails
   Pedro's ≥100 tok/s gate at 88.5 tok/s (`NixOS/meta/decisions.md` ~1440).

---

## 8. Open questions for the lead

1. **Should LongCat-free get its own gate/bearer?** The evidence that it poisons
   a shared bearer is strong (§3). That is a config change, not in this slice.
2. **Is the ~2h zen `Retry-After` a per-day budget?** Two wall events in one
   evening (18:27 → 2h33m, 18:57 → 2h03m) suggests a rolling/daily budget. The
   proxy currently treats it as a hard hold with `retry_probe_required`. Worth
   confirming against opencode's published free-tier terms before relying on
   more than ~2 wall events per evening.
3. **The 200K vs 1M free-tier context gap** — is any fleet lane routinely over
   200K of agentic context? If yes, LongCat-free's window may outweigh its
   benchmark opacity for that specific lane.

---

## Sources

### External (retrieved 19/10/2026 via the NixOS-server browser toolset,
identity `google-search`, per the 05/10/2026 server-only web rule)

- **[X1]** Xiaomi MiMo official release note — *MiMo-V2.6: Scaling Up
  Reinforcement Learning for Self-Improvement*.
  <https://mimo.mi.com/docs/en-US/news/latest/v2-6>
  Read in full via the browser toolset; the benchmark sentences quoted in §1
  were extracted verbatim from this page, including the "Starting RL training
  from MiMo-V2.6-Distill-Qwen-9B" attribution that separates the 9B distill's
  SWE-bench 66.2 from V2.6-Flash's DeepSWE 65.7.
- **[X2]** `mimo.xiaomi.com/mimo-v2-6` — the marketing mirror of [X1]. **JS-
  rendered and returned no extractable text** when read on 19/10; the SWE-bench
  61.1→66.2 figure was encountered there only via Google's AI Overview, which
  mis-attributes it to V2.6-Flash. Cited here solely to record that provenance
  trap — **treat [X1] as authoritative, not [X2].**
- **[A1]** B Xiao et al., *MiMo-V2-Flash Technical Report*, arXiv, Jan 2026 —
  SWE-Bench Verified 73.4%, Multilingual 71.7%, 309B/15B MoE, 27T tokens,
  3-layer MTP 2.6× decoding speedup. Reached via arXiv search result
  ("MiMo-V2-Flash Technical Report, by B Xiao · 2026").
  ⚠️ **This is the V2-series model (Jan 2026), not V2.6 (Sep 2026).** A guessed
  arXiv id (`2601.02352`) was checked and is an unrelated hep-th paper; the
  correct id was not independently confirmed, so cite by title/author, not id.
- **[B1]** Blackbox model card — LongCat-2.5-Preview (MoE ~1.6T/48B, 1M ctx,
  128K out, released 26/09/2026, no published benchmarks).
  <https://www.blackbox.ai/models>
- **[B2]** BenchmarkList — *LongCat 2.5 Preview Benchmark Scores & Evals*:
  "Meituan published no benchmarks for this preview."
  <https://benchmarklist.com/models>
- **[OC1]** OpenCode docs — *Go*. <https://opencode.ai/v2/docs/console/go>
  ($10/mo Go, $40/mo Go Plus; model list; "LongCat 2.5 Preview Free (limited
  time)"; mimo appears only as paid MiMo-V2.6-Flash/-Pro; verified clients.)
- **[OC2]** same as [OC1].
- **[OC3]** OpenCode docs — *Models* (Console / zen).
  <https://opencode.ai/v2/docs/console/models/> (free column "Free/Free/Free"
  for both models.)
- **[OC4]** OpenCode — *LongCat-2.5-Preview vs MiMo-V2.6-Flash* normalized
  capability compare.
  <https://opencode.ai/data/compare/meituan/longcat-2-5-preview/xiaomi/mimo-v2-6-flash>
- **[OC5]** OpenCode — *MiMo-V2.6-Flash model data*.
  <https://opencode.ai/data/xiaomi/mimo-v2-6-flash>
- **[OC6]** OpenCode — *LongCat-2.5-Preview model data*.
  <https://opencode.ai/data/meituan/longcat-2-5-preview>
- **[OC7]** OpenCode — *Model data leaderboard* (updated 19/10/2026 18:55
  GMT-3; retention table). <https://opencode.ai/data> → Leaderboard tab.
- **[V1]** Vault — `3. Resources/🛠️ Tools/opencode-zen-offers-2026-10.md`
  (Reddit sweep 15/10/2026; free-lane rotation, client-lock history).

### Local receipts

- `journalctl --user -u opencode-go-throttle-proxy` and
  `-u opencode-zen-throttle-proxy` (status/model counts, `elapsed_ms`
  percentiles, `aimd-shrink`, `queue-wait-timeout`, 403 credential-reject).
- `curl http://127.0.0.1:8770/metrics`, `:8774/metrics`,
  `/__throttle/health` (token totals, duration sums, AIMD gauges,
  `last_ratelimit`, `retry_after_until`).
- `systemctl --user show opencode-{go,zen}-throttle-proxy -p Environment`.
- `~/NixOS/modules/home/lib/pi-opencode-models.nix` (generated from
  models.dev) + `~/NixOS/user-scripts/gen-pi-opencode-models.py`.
- `~/NixOS/meta/decisions.md` (~1435: ≥100 tok/s gate, glm-5.3-flash 88.5
  FAIL; ~1440).
