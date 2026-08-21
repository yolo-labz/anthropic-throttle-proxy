# Research — statusline data sources (PR #205)

**Measured**: 16/08/2026, 23:18–23:45 BRT, host `desktop` (x86_64-linux).
**Proxy under measurement**: PID 1874, up 10h50m, build
`/nix/store/vrvn7sa0dn5d2fwfj35jhmhn062ljn1i-anthropic-throttle-proxy-0.1.0`.
**Scope**: research only. No source file in this repo or in `~/NixOS` was
modified; every command below is a read or a copy-into-`/tmp` experiment.

The question this document answers is narrow: **what does each statusline
consumer render today, where does each field come from, and which fields would
a per-render consumer need that it cannot get cheaply from what the proxy
publishes now.**

---

## 0. Summary of findings

| # | Finding | Evidence |
|---|---|---|
| 1 | The Claude Code statusline's 5h/7d bars come from the **stdin session JSON**, never from the proxy. The script makes **zero network calls**. | §1.2, §1.3 |
| 2 | Because of #1, the bars describe the **bearer the TUI booted with**, while `THROTTLE_ACCOUNT_ROUTING=budget_paced` rewrites `Authorization` per request. | §1.4 |
| 3 | `pi-footer-grid.nix` is a **layout engine with no data source at all** — pure functions over strings. The budget data comes from `pi-parity-extension.nix`, which curls `/__throttle/health`. | §2.1, §2.2 |
| 4 | pi's budget cell is a **fleet aggregate refreshed every 90 s**, deliberately not a per-render reading. | §2.3 |
| 5 | The health blob is **72,643 B** and **growing ~131 B/min**, because `clients` is keyed by ephemeral TCP port and is never pruned. 84 % of it is that map. | §3.2, §3.3 |
| 6 | Health is **fast** (p50 2.69 ms) but **big**; the cost is bytes+parse, not latency. | §3.1 |
| 7 | The statusline render itself already costs **717 ms** and spawns **20–34 `python3` processes**. A 3 ms curl is ~0.4 % of that — cost is not the blocker; **shape and correctness** are. | §1.5, §4.2 |
| 8 | Five things a correct per-render bar needs are **not derivable** from the current payload without re-implementing proxy internals: serving-account selection, live-vs-stale window view, binding window choice, queue-vs-throttle distinction, and a label for the account. | §4.1 |

---

## 1. Claude Code statusline

### 1.1 Wiring

`~/NixOS/modules/home/claude-code.nix:1938-1941`:

```nix
      # Rich status line with emoji, progress bars, and host/git context
      statusLine = {
        type = "command";
        command = "bash ${statusline-script}";
      };
```

Live, as Claude Code actually reads it:

```
$ jq -c '.statusLine' ~/.claude/settings.json
{"command":"bash /nix/store/3wfiggzcx0ca84qpyd9kpy0xhh88ll61-claude-statusline.sh","type":"command"}
```

The script is defined at `~/NixOS/modules/home/claude-code.nix:1331`:

```nix
  statusline-script = pkgs.writeShellScript "claude-statusline.sh" ''
        input=$(cat)
```

The **only** input is stdin. Claude Code pipes the session JSON in; the script
reads it with `input=$(cat)` and never reads a file or socket for state.

### 1.2 Every field, and where it comes from

The whole parse is one `jq` call over stdin,
`~/NixOS/modules/home/claude-code.nix:1519-1532`:

```nix
        # ── Parse JSON ──────────────────────────────────────────────
        eval "$(echo "$input" | ${pkgs.jq}/bin/jq -r '
          @sh "work_dir=\(.workspace.current_dir // "")",
          @sh "ctx_pct=\(.context_window.used_percentage // "")",
          @sh "model_name=\(.model.display_name // "")",
          @sh "effort=\(.effort_level // "")",
          @sh "session_id=\(.session_id // "")",
          @sh "cost_usd=\(.cost.total_cost_usd // "")",
          @sh "duration_ms=\(.cost.total_duration_ms // "")",
          @sh "five_pct=\(.rate_limits.five_hour.used_percentage // "")",
          @sh "five_reset=\(.rate_limits.five_hour.resets_at // "")",
          @sh "week_pct=\(.rate_limits.seven_day.used_percentage // "")",
          @sh "week_reset=\(.rate_limits.seven_day.resets_at // "")"
        ')"
```

| Rendered cell | Variable | Source | Definition site |
|---|---|---|---|
| 🐧 Host | `host_name` | `hostname -s`, local | `claude-code.nix:1536-1542` |
| 📁 Project | `project` | `basename "$work_dir"` from `.workspace.current_dir` | `claude-code.nix:1650-1654` |
| 🌿 Branch ±N | `branch`, `dirty`, `cnt` | `git -C "$work_dir" branch --show-current` + `status --porcelain`, local | `claude-code.nix:1545-1560` |
| 📊 Ctx bar | `ctx_pct` | **stdin** `.context_window.used_percentage` | `claude-code.nix:1522` |
| 🧭 5h bar | `five_pct`, `five_reset` | **stdin** `.rate_limits.five_hour.{used_percentage,resets_at}` | `claude-code.nix:1528-1529` |
| 📅 7d bar | `week_pct`, `week_reset` | **stdin** `.rate_limits.seven_day.{used_percentage,resets_at}` | `claude-code.nix:1530-1531` |
| ±% pace tag | `hourly_tag` / `daily_tag` | derived: `budget_delta()` compares `pct` against elapsed fraction of the window | `claude-code.nix:1629-1653` |
| → reset clock | — | `fmt_time` / `fmt_weekday` over the stdin epoch | `claude-code.nix:1491-1503` |
| 🤖 Model (+🪶/⚠) | `model_name`, `intent_model` | stdin `.model.display_name`, compared to `.model` in the project's `.claude/settings.local.json` | `claude-code.nix:1523`, `1576-1590` |
| 🔥 Effort | `effort` | stdin `.effort_level` | `claude-code.nix:1524` |
| 💎 Session name + dur | `session_id`, `duration_ms` | stdin, hashed to an adjective-animal via `gen_session()` | `claude-code.nix:1505-1517` |
| Bars themselves | — | `bar()` / `bar_compact()`, pure ANSI over a percent | `claude-code.nix:1459-1489` |
| Layout tier | `COLS` | `CLAUDE_STATUS_COLS` → `COLUMNS` → `stty size` → 120 | `claude-code.nix:1357-1374`; tiers at `1812` (<95), `1825` (<145), else full |

### 1.3 Hard evidence: the statusline never talks to the proxy

Grepping the **live store script** (not the Nix source) for every network
primitive returns nothing:

```
$ S=/nix/store/3wfiggzcx0ca84qpyd9kpy0xhh88ll61-claude-statusline.sh
$ grep -cE 'curl|wget|/dev/tcp|__throttle|8765|http://' "$S"
0
```

The complete set of external binaries it can invoke:

```
$ grep -oE '/nix/store/[a-z0-9]+-[a-zA-Z0-9.+-]+/bin/[a-z0-9_.-]+' "$S" | sort -u
/nix/store/1bp02949k0xdihbgphpwbzba1741pknk-jq-1.8.2-bin/bin/jq
/nix/store/1k2lblqlj39azh6wn1sffa2869vrg3mr-git-2.54.0/bin/git
/nix/store/cp7wjv1pl4wapfk48svvizxd089v9h0a-coreutils-9.11/bin/date
/nix/store/jxyrvv4gbpnp3ap5iy7wxwl1sg4x2x88-python3-3.14.6/bin/python3
/nix/store/v8llyqw71lygr2llhmcc8ya5bdlzq45v-bash-5.3p9/bin/bash
```

`jq`, `git`, `date`, `python3`, `bash`. No HTTP client exists in the closure.

Driving the live script with a synthetic session JSON, with and without
`.rate_limits`, isolates the dependency exactly — the 5h and 7d cells vanish
and the columns collapse to empty separators:

```
$ printf '%s' "$json" | CLAUDE_STATUS_COLS=160 bash "$S"
🐧 Desktop        │ 🌿 205-statusline ok           │ 🧭 5h ████░░░░░░░░░░░  24% −62% → 00:06 │ 🤖 Opus 5 (1M context) 🔥 high
📁 205-statusline │ 📊 Ctx ███████░░░░░░░░░░░  44% │ 🚨 7d ████████████░░░  85% +48% → sex 09:00 │ 💎 free-owl 15m

$ printf '%s' "$json" | jq -c 'del(.rate_limits)' | CLAUDE_STATUS_COLS=160 bash "$S"
🐧 Desktop        │ 🌿 205-statusline ok           │  │ 🤖 Opus 5 (1M context) 🔥 high
📁 205-statusline │ 📊 Ctx ███████░░░░░░░░░░░  44% │  │ 💎 free-owl 15m
```

(ANSI stripped for legibility; the raw escape-laden output is what the terminal
receives.)

### 1.4 Why that source is the wrong one for this fleet

The proxy is configured to route per request, not per session. Live unit
environment, PID 1874:

```
$ systemctl --user show anthropic-throttle-proxy.service -p Environment --value | tr ' ' '\n' | grep -E 'ACCOUNT|UTILIZATION|QUEUE_MODE'
THROTTLE_ACCOUNT_CRED_PATHS=A:/home/notroot/.claude/.credentials.json,B:/home/notroot/.claude-b/.credentials.json,C:/home/notroot/.claude-c/.credentials.json
THROTTLE_ACCOUNT_ROUTING=budget_paced
THROTTLE_QUEUE_MODE=fair
THROTTLE_UTILIZATION_TARGET=0.900000
```

`_route_account_if_enabled` (`src/anthropic_throttle_proxy/proxy.py:1443-1459`)
rewrites the upstream `Authorization` header for every `POST /v1/messages`:

```python
def _route_account_if_enabled(
    ...
) -> tuple[str, str | None]:
    """Optionally rewrite upstream Authorization to a configured account.
    ...
    """
    if method != "POST" or "v1/messages" not in path:
        return incoming_bid, None
```

So `.rate_limits` in the session JSON describes the credential the TUI
authenticated with at launch; the request it is about to make may be served by
a different one of the three. Three accounts were live and materially different
at 23:39:

```
$ curl -s http://127.0.0.1:8765/__throttle/health | jq -r '.bearers | to_entries[] | "\(.key)\t\(.value.unified // "null" | tojson)"'
b144f62f	{"status":"allowed_warning","reset":1787306400,"representative_claim":"seven_day","util_5h":0.5,"status_5h":"allowed","reset_5h":1786950600,"util_7d":0.89,"status_7d":"allowed_warning","reset_7d":1787306400}
47f0b262	{"status":"rejected","reset":1786953600,"representative_claim":"seven_day","util_5h":0.0,"status_5h":"allowed","reset_5h":1786915200,"util_7d":1.0,"status_7d":"rejected","reset_7d":1786953600}
666a53af	{"status":"allowed","reset":1786950600,"representative_claim":"five_hour","util_5h":0.51,"status_5h":"allowed","reset_5h":1786950600,"util_7d":0.72,"status_7d":"allowed","reset_7d":1787346000}
_anon	"null"
api-key	"null"
```

One bearer is 7d-`rejected` at 1.0, one is `allowed_warning` at 0.89, one is
`allowed` at 0.72. A statusline reading the launch-time bearer can render any
of those three numbers regardless of which account will serve the next call.

### 1.5 What a render already costs

Ten renders of the live script, fixed width, warm cache:

```
$ time (for i in $(seq 1 10); do printf '%s' "$json" | CLAUDE_STATUS_COLS=160 bash "$S" >/dev/null; done)
real	0m7,171s
user	0m5,473s
sys	0m1,765s
```

**717 ms per render.** The dominant cost is `python3` process spawns for the
wcwidth-aware `vlen()` (`claude-code.nix:1377`) and `trunc()`
(`claude-code.nix:1410`) helpers. Counted
by copying the script to `/tmp` and interposing a counting shim on the store
python3 path (original untouched):

```
python3 spawns in ONE render (COLS=160): 20
python3 spawns in ONE render (COLS=90):  34
```

This matters for §4: **a per-render HTTP call is not what would make this
script slow.** It is already slow, and 3 ms of curl is ~0.4 % of the existing
budget. The blocker is payload shape and correctness, not latency.

---

## 2. pi's footer grid

### 2.1 `pi-footer-grid.nix` has no data source

`~/NixOS/modules/home/lib/pi-footer-grid.nix` (142 lines) exports exactly two
things, both pure:

```
$ grep -n "export const" ~/NixOS/modules/home/lib/pi-footer-grid.nix
46:  export const createFooterGrid = ({ sep, ellipsis, visibleWidth, truncateToWidth }) => {
124:  export const gauge = (pct, cells, cursor) => {
```

`createFooterGrid` takes `{sep, ellipsis, visibleWidth, truncateToWidth}` and
returns `(columns, room) => string[]`. `gauge(pct, cells, cursor)` maps a
percentage to an array of `{ch, kind}` cells. Neither reads a file, an env var,
or a socket. The module header states the contract
(`pi-footer-grid.nix:14-19`):

> Every cell here renders to a shape whose width is knowable in advance: a
> gauge is always `GAUGE_CELLS` wide, a percentage is always 3-4 characters,
> and a token count is humanised to at most 6.

**So "what the footer grid renders" is answered one file up**: the grid is
told what to draw.

### 2.2 The seven columns and their real sources

The caller is `~/NixOS/modules/home/lib/pi-parity-extension.nix:1997-2006`:

```js
                const lines = grid(
                  [
                    { key: "id", priority: 10, top: harness, bot: roleCell },
                    { key: "ctx", priority: 9, top: ctxCell, bot: ctxDetail },
                    { key: "budget", priority: 8, top: budgetCell, bot: budgetDetail },
                    { key: "place", priority: 5, max: 34, top: cwdCell, bot: branchCell },
                    { key: "model", priority: 4, max: 26, top: modelCell, bot: effortCell },
                    { key: "cache", priority: 2, top: hitCell, bot: cacheCell },
                    { key: "econ", priority: 1, top: costCell, bot: flowCell ?? bearerCell },
                  ],
                  width,
                );
```

| Column | Top / Bottom | Source | Site |
|---|---|---|---|
| `id` | harness badge / role | `PI_INTENT_MODEL`, `PI_LIVE_MODEL`, `PI_ROLE_RESOLVED` env | `pi-parity-extension.nix:2075-2085` |
| `ctx` | context gauge+% / `tokens/window` | `ctxRef.getContextUsage()` — pi's own in-process session state | `:1923-1933` |
| **`budget`** | **7d gauge + `7d NN%`** / **`⏳ Nh` + `5h NN%`** | **`curl :8765/__throttle/health`** | `:1935-1957` |
| `place` | cwd / branch ±N | `ctxRef.cwd`; `git status --porcelain` spawn | `:1908-1920`, `:1633-1647` |
| `model` | model+provider / effort | pi's model registry, in-process | `:1895-1905` |
| `cache` | `hit N%` / `R… W…` | `tally()` over `sessionManager.getEntries()` | `:1685-1706` |
| `econ` | `$cost` / `↑in ↓out` **or** `👥serving/total` | session tally; the `👥` fallback is proxy-derived | `:1985-1991` |

Only **two** of the seven touch the proxy, and both come from the same fetch.

### 2.3 The budget fetch, and its cadence

`anthropicBearers` (`pi-parity-extension.nix:1435-1464`) shells out to `curl`,
following the ingress → lane hop:

```js
    const fetchHealth = async (cwd: string, url: string) => {
      const probe = await pi.exec("curl", ["-fsS", "--max-time", "3", `${url}/__throttle/health`], { cwd, timeout: 4_000 });
```

It keeps only bearers that have reported a window (`:1459-1461`):

```js
      const bearers = Object.entries(health.bearers ?? {})
        .map(([id, b]) => ({ id, unified: (b as { unified?: Unified }).unified, unifiedAt: (b as { unified_at?: number }).unified_at }))
        .filter((b): b is { id: string; unified: Unified; unifiedAt?: number } => !!b.unified && b.unified.status_7d != null);
```

`summarizeBudget` (`~/NixOS/modules/home/lib/pi-budget-state.nix:239-262`)
reduces those to one fleet reading — **the minimum utilization among serving
bearers**, not any single account:

```js
  export const summarizeBudget = (bearers, nowMs = Date.now()) => {
    if (bearers.length === 0) return { kind: "silent" };
    const serving = bearers.filter((b) => bearerUsable(b.unified, nowMs));
    if (serving.length === 0) return { kind: "capped", total: bearers.length };
    const best = (key) => Math.min(...serving.map((b) => Math.round(Number(b.unified[key] ?? 0) * 100)));
```

`bearerUsable` (`pi-budget-state.nix:43-45`) is where the staleness rule lives:

```js
  export const bearerUsable = (unified, nowMs = Date.now()) =>
    windowOpen(unified, "status_7d", "reset_7d", nowMs) &&
    windowOpen(unified, "status_5h", "reset_5h", nowMs);
```

Refresh cadence is **not** per render — `pi-parity-extension.nix:2046-2061`:

```js
    const BUDGET_POLL_MS = 90_000;
```

with the reason stated inline (`:2036-2042`): a turn-boundary-only refresh
froze idle panes, so a 90 s timer was added. On the turn-boundary path
(`:2062-2068`) `refreshBudget` is fired but never awaited, for the reason given
at `:1625-1629` — *"this is decoration, and `agent_end` is the moment the user
gets the turn back."*

**Conclusion for (b):** pi already solves the identity problem the Claude Code
statusline has — it reads the proxy — but it pays for the 72 KB blob by polling
at 1/90 Hz and by rendering a fleet aggregate rather than "the account that
will serve me". It also had to re-implement liveness (`bearerUsable`),
warning (`bearerWarning`), and pace (`elapsed7`) client-side.

---

## 3. What the proxy publishes, and why neither consumer can use it cheaply

### 3.1 Size and latency of `/__throttle/health`

The exact command from the brief, run at 23:39:

```
$ curl -s -w '%{size_download} %{time_total}\n' http://127.0.0.1:8765/__throttle/health -o /dev/null
72579 0.003645
```

60 consecutive reads at 23:44:

```
n=60  min=2.37ms  p50=2.69ms  p95=2.92ms  max=2.99ms
size: min=72643 max=72643
```

Health is **well inside invariant #4** (<50 ms). The problem is bytes and
parse, not latency.

### 3.2 84 % of the payload is a map no consumer wants

```
$ jq -r 'to_entries | map({k:.key, bytes:(.value|tojson|length)}) | sort_by(-.bytes) | .[] | "\(.bytes)\t\(.k)"' health.json
61062	bearers
130	build
86	account_identity
74	api_key
40	brake
27	upstream
18	upstream_egress_last_check
9	central_status
7	version
6	queue_mode
4	served
...
```

Inside the largest bearer:

```
$ jq -r '.bearers.b144f62f | to_entries | map({k:.key, bytes:(.value|tojson|length)}) | sort_by(-.bytes) | .[] | "\(.bytes)\t\(.k)"' health.json
17811	clients
809	limiter
484	last_ratelimit
208	unified
18	unified_at
16	_util_shrink_key
15	_util_warn_key
4	served
1	inflight
1	queued
```

`clients` is 17,811 of that bearer's 19,503 bytes. Across all bearers:

```
$ jq -r '.bearers | to_entries[] | "\(.key)\t clients=\((.value.clients // {}) | length)"' health.json
b144f62f	 clients=323
47f0b262	 clients=28
666a53af	 clients=344
_anon	 clients=315
api-key	 clients=0
```

Each entry is a dead ephemeral socket with three zeroed counters:

```
$ jq -r '.bearers.b144f62f.clients | to_entries[0] | "key: \(.key)\nval: \(.value|tojson)"' health.json
key: 127.0.0.1:39994
val: {"queued":0,"inflight":0,"served":0}
```

**The five fields a bar actually needs total 208 bytes** (`unified`) — 0.29 %
of the payload.

### 3.3 The blob grows while you watch it

The `clients` map is written at
`src/anthropic_throttle_proxy/proxy.py:3577` and there is no prune path:

```
$ grep -rn "clients\b" src/anthropic_throttle_proxy/proxy.py | grep -iE "pop|del |prune|evict|clear|maxlen|setdefault"
3577:        cstate = bstate["clients"].setdefault(cid, {"queued": 0, "inflight": 0, "served": 0})
```

Measured growth, one read per minute:

```
23:31:38  bytes=71695  client_entries=1049
23:32:38  bytes=71826  client_entries=1053
23:33:38  bytes=72156  client_entries=1061
```

≈ **131 B/min**, ≈ 52 B per new client key. The key is `host:port`, so every
reconnect mints another. A per-render consumer bound to this collection has its
cost set by an unrelated, monotonically growing map.

### 3.4 What is present but requires proxy knowledge to read correctly

These are published, and are the reason a naive consumer gets it wrong:

- **`unified` per bearer** — but a reading can outlive its own window.
  Live at 23:20, `47f0b262` reported `status_5h=allowed util_5h=0.0` from a
  snapshot 606 minutes old whose `reset_5h` had already passed:

  ```
  $ now=$(date +%s); jq -r --argjson now "$now" '.bearers | to_entries[] | select(.value.unified_at) | "\(.key)\tunified_at=\(.value.unified_at)\tage_s=\((($now - .value.unified_at)|floor))"' health.json
  b144f62f	unified_at=1786933241.0565608	age_s=442
  47f0b262	unified_at=1786897309.9776423	age_s=36374
  666a53af	unified_at=1786933241.382773	age_s=442
  ```

- **`limiter` per bearer** — has `inflight`, `max_concurrent`, `queued_total`,
  `retry_after_until`, `queued_per_client`, `storm_mode`. That is the
  queue-vs-throttle answer, but it arrives as 809 bytes of scheduler internals
  per bearer, including the whole RR order:

  ```
  $ jq -c '.bearers.b144f62f.limiter' health.json
  {"inflight":5,"max_concurrent":5,"hard_max":5,"queue_mode":"fair","queue_enabled":true,"observe_enabled":true,"last_throttle_at":1786919182.565376,"successes_since_throttle":216,"retry_after_until":1786894931.3557084,"retry_probe_required":false,"retry_probe_inflight":false,"retry_probe_blocks_routing":false,"queued_total":9,"priority_inflight":0,"priority_queued":0,"queued_per_client":{"127.0.0.1:56178":1,...},"rr_order":[...],"recent_shrinks":0,"storm_mode":false,"effective_ramp_after":6}
  ```

- **`last_ratelimit`** — the raw upstream headers, duplicating `unified`
  as strings:

  ```
  $ jq -c '.bearers.b144f62f.last_ratelimit' health.json
  {"anthropic-ratelimit-unified-status":"allowed_warning","anthropic-ratelimit-unified-reset":"1787306400","anthropic-ratelimit-unified-representative-claim":"seven_day","anthropic-ratelimit-unified-5h-status":"allowed","anthropic-ratelimit-unified-5h-utilization":"0.24","anthropic-ratelimit-unified-5h-reset":"1786950600","anthropic-ratelimit-unified-7d-status":"allowed_warning","anthropic-ratelimit-unified-7d-utilization":"0.85","anthropic-ratelimit-unified-7d-reset":"1787306400"}
  ```

### 3.5 The other surfaces, and why they don't help either

| Surface | Size / latency | Why it doesn't serve a statusline |
|---|---|---|
| `GET /metrics` (`proxy.py:4324`) | 17,642 B, 3.2 ms | Prometheus text; only 6 utilization gauges among ~400 lines; **no reset epochs, no status strings, no account labels**. See below. |
| `GET /ui/stats` (`ui/routes.py:969`) | 23,622 B, 5.6 ms | HTML fragment for HTMX. Carries the derived verdict *and* labels — but as markup, unparseable without scraping. |
| `GET /__throttle/health` on ingress `:8760` | 716 B, 1.2 ms | Lane status only — **`bearers` is absent**, which is exactly the hop pi documents at `pi-parity-extension.nix:1411-1419`. |
| `GET /` root probe (`proxy.py:4322`) | — | Local 200, no state. |

Metrics carries utilization but drops everything else:

```
$ grep '^anthropic_ratelimit_unified' metrics.txt
anthropic_ratelimit_unified_5h_utilization{bearer="47f0b262"} 0.0
anthropic_ratelimit_unified_5h_utilization{bearer="b144f62f"} 0.27
anthropic_ratelimit_unified_5h_utilization{bearer="666a53af"} 0.28
anthropic_ratelimit_unified_7d_utilization{bearer="47f0b262"} 1.0
anthropic_ratelimit_unified_7d_utilization{bearer="b144f62f"} 0.85
anthropic_ratelimit_unified_7d_utilization{bearer="666a53af"} 0.69
anthropic_ratelimit_unified_warnings_total{bearer="b144f62f",window="5h"} 2.0
...
```

No `reset_5h`, no `status_7d`, no label. A bar built on this cannot draw a
countdown or distinguish `allowed_warning` from `rejected`.

Ingress, for completeness:

```
$ curl -s -w '\nSIZE=%{size_download} TIME=%{time_total}\n' http://127.0.0.1:8760/__throttle/health
{"status": "ok", "ingress": true, "default_lane": "http://127.0.0.1:8765", "host": "127.0.0.1", "port": 8760, "served": 4125, ...,
 "lanes": {"codex": {...}, "deepseek": {...}, "anthropic": {"open": true, "detail": "ok", ...}}}
SIZE=716 TIME=0.001193
```

---

## 4. The gap, stated precisely

### 4.1 Fields a per-render statusline needs and cannot get cheaply today

| # | Needed field | Why it is needed | Why not available cheaply now |
|---|---|---|---|
| 1 | **The bearer that would serve the next request** | Routing is per request (`proxy.py:1443`), so any other account's numbers are the wrong numbers | Not published at all. Reproducing it means re-implementing `_account_routing_candidate_score` — including the scoped per-model weekly meter and the `now`-consistent snapshot (`proxy.py:1476-1481`) — in shell |
| 2 | **A live-viewed window** (stale readings dropped) | `47f0b262` published `util_5h=0.0 allowed` from a 606-minute-old snapshot past its own reset (§3.4) | `unified` + `unified_at` are published raw; the liveness rule exists inside the proxy and was re-implemented by pi in `pi-budget-state.nix:43-45`. A shell consumer would be the third implementation |
| 3 | **Which window is binding**, and its utilization | 5h and 7d disagree constantly — live: `b144f62f` 5h 0.50 / 7d 0.89; `666a53af` 5h 0.51 / 7d 0.72 | `_binding_utilization` / `_binding_window` exist in-proxy (used at `ui/routes.py:190-191`) but are not on any JSON surface |
| 4 | **Queued vs throttled**, and for how long | "My request is slow" has two different answers and two different actions | Derivable only from `limiter.queued_total` + `retry_after_until` + `storm_mode` — 809 B/bearer of scheduler internals, plus the `_bearer_pacing_state` logic that is called at `ui/routes.py:184` and renders only to HTML |
| 5 | **A human label for the account** (`A`/`B`/`C`) | `b144f62f` is not something Pedro can act on; `B` is | `accounts.bearer_labels()` (`accounts.py:429-431`) has exactly this map, and its **only** consumer is the HTML dashboard (`ui/routes.py:650`). It is absent from `/__throttle/health` and from `/metrics` |

Two secondary gaps, cheaper but real:

- **Credential quarantine** is published per bearer, but only for the API-key
  slot in this snapshot (`{"detail":"invalid x-api-key","ok":false,"status":401}`),
  and a restart moves it into `_restored_credentials` — the health handler
  already special-cases this at `proxy.py:4148-4150`. A consumer would need to
  know that.
- **Reset countdown as a duration.** `ui/routes.py:670-672` already notes that
  `148806` in a retry-after column is a number the operator must divide by 3600
  mid-incident. Both consumers currently re-derive it (`fmt_time` at
  `claude-code.nix:1491`; `resetHours` at `pi-budget-state.nix:248-251`).

### 4.2 Cost, measured, for the record

| Path | Cost | Command |
|---|---|---|
| One health fetch | 72,643 B, p50 2.69 ms | §3.1 |
| curl + `jq` extracting only the unified blocks, 5× | 0.078 s → **15.6 ms/iteration** | `time (for i in 1 2 3 4 5; do curl -s .../health \| jq -r '[.bearers[]\|select(.unified)\|{u5:.unified.util_5h,u7:.unified.util_7d,s:.unified.status}]\|@json' >/dev/null; done)` |
| curl alone | 0.071 s / 5 → 14.2 ms | `time (for i in 1 2 3 4 5; do curl -s -o /dev/null .../health; done)` |
| `jq` alone over the saved 72 KB | 0.032 s / 5 → 6.4 ms | `time (for i in 1 2 3 4 5; do jq -r '.bearers[].unified' health.json >/dev/null; done)` |
| One full statusline render (existing) | **717 ms**, 20–34 `python3` spawns | §1.5 |

Read carefully: **15.6 ms of curl+jq against a 717 ms render is 2 %.** The
argument for a smaller surface is *not* that the current script cannot afford
3 ms. It is:

1. **Fan-out.** 23 `claude` processes were live during measurement
   (`pgrep -fc claude` → 23; `ss -tn 'sport = :8765'` → 15 established). Claude
   Code re-renders on a sub-second cadence, so N panes × render rate × 72 KB is
   the real number, and it grows 131 B/min on its own.
2. **Correctness.** Four of the five fields in §4.1 are *derivations*, not
   lookups. Shipping them as raw material means every consumer re-implements
   proxy semantics — which has already happened once (pi's `bearerUsable`,
   `summarizeBudget`) and would happen a second time in `bash` + `jq`, in a
   script that spawns 34 processes to measure string width.

---

## 5. Anti-claims (things this research does **not** establish)

- It does not show the current statusline is *slow because of the proxy* — it
  makes no proxy calls at all (§1.3), and its 717 ms is entirely local.
- It does not show `/__throttle/health` is slow. p95 is 2.92 ms (§3.1);
  invariant #4 holds comfortably.
- It does not measure real Claude Code statusline invocation frequency. The
  fan-out arithmetic in §4.2 uses process counts, not an observed render rate;
  no journal or trace was collected that would prove the per-second cadence.
- The `~68 KB` figure in the task brief measured low: actual was **69,492 B at
  23:20** and **72,643 B at 23:44**. Of the 5 `bearers` keys, only **3** carry
  unified windows — `_anon` and `api-key` publish `"unified": null` (§1.4).

## 6. Verification commands (all read-only, reproducible)

```sh
# statusline: source, wiring, and the network-free proof
sed -n '1331p;1519,1532p;1938,1941p' ~/NixOS/modules/home/claude-code.nix
S=$(jq -r '.statusLine.command' ~/.claude/settings.json | awk '{print $2}')
grep -cE 'curl|wget|/dev/tcp|__throttle|8765|http://' "$S"          # expect 0

# pi footer: layout has no data; the extension has the fetch
grep -n "export const" ~/NixOS/modules/home/lib/pi-footer-grid.nix   # 2 pure fns
sed -n '1420,1424p;1997,2006p;2046p' ~/NixOS/modules/home/lib/pi-parity-extension.nix
sed -n '43,45p;239,262p' ~/NixOS/modules/home/lib/pi-budget-state.nix

# proxy: size, growth, and the 208 bytes that matter
curl -s -w '%{size_download} %{time_total}\n' -o /tmp/h.json http://127.0.0.1:8765/__throttle/health
jq -r 'to_entries|map({k:.key,bytes:(.value|tojson|length)})|sort_by(-.bytes)|.[]|"\(.bytes)\t\(.k)"' /tmp/h.json
jq -r '.bearers|to_entries[]|"\(.key)\tclients=\((.value.clients//{})|length)\tunified=\((.value.unified|tojson|length))"' /tmp/h.json
```

---

**Ownership note.** `specs/205-statusline/spec.md` was authored in this
worktree in parallel with this research (both dated 16/08/2026). Its
"Problem evidence" table cites the same measurements taken minutes apart —
69,408 B @23:19 vs 69,492 B @23:20, 1,006 vs 1,010 client entries — the drift
is the growth documented in §3.3, not a contradiction. Where the two differ in
digits, **this file's numbers are the ones re-verified at 23:39–23:45 BRT**
with the exact commands pasted above.
