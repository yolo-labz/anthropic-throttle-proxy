# Feature Specification: Compact `GET /__throttle/statusline` Render Probe

**Feature Branch**: `205-statusline`
**Created**: 16/08/2026
**Status**: Draft
**Input**: The proxy is the only component that knows every bearer's live unified 5h/7d windows, but it publishes them only inside the ~70 KB `/__throttle/health` blob. The Claude Code statusline (`~/NixOS/modules/home/claude-code.nix:1331+`) therefore reads its quota numbers from the stdin session JSON instead, which describes the account the TUI was launched with — not the account the proxy actually routed to.

## The single question

> **Which account will serve my next request, how full is its binding window, when does that window reset, and am I queued or throttled right now?**

Everything below exists to answer that sentence in one request, in one parse, at render cadence. A field that does not help answer it is out of scope by construction.

## Problem evidence (measured 16/08/2026 23:19–23:37 BRT, desktop, PID 1874)

| Fact | Measurement | Command |
|---|---|---|
| Health payload size | **69,408 B** @23:19 → **72,695 B** @23:37 | `curl -q -fsS -o /dev/null -w "%{size_download}\n" http://127.0.0.1:8765/__throttle/health` |
| Health latency | **2.3–2.9 ms**; p95 **2.73 ms** over 60 runs | same, `-w '%{time_total}'` |
| `bearers` share of payload | **60,995 / 69,408 B = 88 %** | `jq 'to_entries\|map({k:.key,bytes:(.value\|tostring\|length)})\|sort_by(-.bytes)'` |
| `clients` map entries | **1,006** @23:19 → **1,069** @23:37, ephemeral `127.0.0.1:PORT` keys | `jq '[.bearers[].clients//{}\|length]\|add'` |
| Concurrent statusline callers | **35** `claude` processes, **28** live TCP clients on `:8765` | `pgrep -fc claude` / `ss -tn 'sport = :8765'` |

**The blob grows while you watch it.** Across the 25 minutes of this drafting session the payload gained **+3,287 bytes** as `clients` gained **+63 entries** (~131 B/min, ~52 B per tracked client) — keyed by ephemeral TCP port, so every reconnect mints a new one. A per-render consumer must not be coupled to a collection with that growth shape; this is the direct justification for FR-003.

At 35 panes rendering on Claude Code's ~300 ms statusline cadence, polling health would serialize **8.1 MB/s of JSON** (35 × 3.33 Hz × 69,408 B) to answer a question worth 322 bytes. The same fan-out against this endpoint is **37.6 KB/s** — a **216×** reduction. That is the whole feature.

### The correctness half (why a smaller health blob is not enough)

Two live facts show the statusline cannot just read raw fields:

1. **Routing is per-request and model-aware.** `THROTTLE_ACCOUNT_ROUTING=budget_paced` (live env, PID 1874) rewrites the upstream `Authorization` per `POST /v1/messages`; `_account_routing_candidate_score` (`src/anthropic_throttle_proxy/proxy.py:1234-1349`) folds the account's **scoped per-model weekly meter** into the ranking when a `model` is supplied. So the session JSON's `rate_limits.five_hour` — which the statusline reads today at `claude-code.nix:1528-1531` — describes the launch-time bearer, not the serving one.
2. **A raw `unified` block can describe a window that no longer exists.** Live: bearer `47f0b262` carried `status_5h=allowed util_5h=0.0` from a snapshot **603 minutes old**, whose `reset_5h` had passed **306 minutes earlier**. `routing.unified_live_view` (`routing.py:287-324`) already encodes the rule — a reading past its own reset epoch must not gate anything — after the 31/07/2026 incident. A naive consumer reading `.bearers[].unified` would render a dead window as healthy.

```sh
# reproduces both facts
now=$(date +%s); curl -q -fsS http://127.0.0.1:8765/__throttle/health | jq -r --argjson now "$now" \
  '.bearers|to_entries[]|select(.value.unified)|
   "\(.key) 5h=\(.value.unified.util_5h)/\(.value.unified.status_5h) reset_5h_in=\(((.value.unified.reset_5h//0)-$now)/60|floor)m age=\((($now-(.value.unified_at//0))/60)|floor)m"'
```

## User Scenarios & Testing

### User Story 1 — A pane shows the account that will actually serve it (Priority: P1)

A statusline render asks the local proxy which account its next request would land on, and renders that account's binding window fullness and reset. The number on screen describes the credential the proxy will actually spend, not the one the TUI booted with.

**Why this priority**: This is the entire premise. A quota bar that names the wrong account is worse than no bar — it is confidently wrong, and Pedro routes real work off it.

**Independent Test**: With ≥2 configured accounts at different utilizations, assert the endpoint names the same bearer that `_account_routing_candidate_score` ranks best for the same `(now, model)` inputs, and that the reported `util`/`window` equal `_binding_utilization`/`_binding_window` over the **live-viewed** unified block.

**Acceptance Scenarios**:

1. **Given** two usable accounts with different binding utilizations, **when** the endpoint is read, **then** `account.bearer` equals the lowest-scoring candidate and `account.util` equals that bearer's binding-window utilization.
2. **Given** the caller passes `?model=<id>` matching an account's scoped weekly meter, **when** the endpoint is read, **then** selection reflects the scoped meter exactly as the hot path would rank it.
3. **Given** the selected bearer's binding window reset epoch has already passed, **when** the endpoint is read, **then** `account.stale` is `true` and the dead window never presents as fresh capacity.
4. **Given** `THROTTLE_ACCOUNT_CRED_PATHS` is unset (central tier), **when** the endpoint is read, **then** `account.label` is `null` and every other field still resolves.

---

### User Story 2 — A pane can tell "queued" from "throttled" without opening a dashboard (Priority: P1)

A render distinguishes *the proxy is admitting me but I am behind other work* from *upstream has hard-paused this account until an epoch*, and shows how long the current condition has held.

**Why this priority**: These two states demand opposite human responses — wait vs. switch account or stop. Live evidence shows both coexisting: bearer `666a53af` sat at `queued_total=12` while `47f0b262` held `retry_after_until` **19,795 s in the future**.

**Independent Test**: Drive a limiter into each state and assert the `state` token, `queue.depth`, and `blocked_until` transition independently; assert `state_since_s` reports the transition timestamp, not the last poll.

**Acceptance Scenarios**:

1. **Given** the selected account has queued work and no active pause, **when** the endpoint is read, **then** `state` is `queued`, `queue.depth` > 0, and `blocked_until` is `null`.
2. **Given** the selected account has `retry_after_until` in the future or a `rejected` binding window, **when** the endpoint is read, **then** `state` is `throttled` and `blocked_until` carries the epoch.
3. **Given** the state is unchanged across repeated polls, **when** the endpoint is read every 300 ms, **then** `state_since_s` grows monotonically from the transition, matching `history.level_since` semantics (`history.py:126-137`).
4. **Given** every configured account scores unusable, **when** the endpoint is read, **then** `state` is `exhausted` and `fleet.usable` is `0`.

---

### User Story 3 — Render-cadence polling is cheaper than the question it replaces (Priority: P2)

35 panes may poll this endpoint continuously without measurably degrading the proxy or the load-bearing health probe.

**Why this priority**: Invariant #4 gives health a hard <50 ms budget because Dokku's healthcheck depends on it. A new endpoint polled two orders of magnitude more often must not be the thing that breaks it.

**Independent Test**: Measure p95 over ≥200 sequential requests against both endpoints on the same process; assert the statusline p95 is under 50 ms **and** strictly below health's, with a payload ≤ 1024 bytes.

**Acceptance Scenarios**:

1. **Given** a process with 1,000+ tracked client entries, **when** the endpoint is read, **then** payload size is independent of client count (O(1), not O(clients)).
2. **Given** sustained polling at render cadence, **when** health is probed concurrently, **then** health p95 stays inside its existing budget.
3. **Given** upstream egress is down, **when** the endpoint is read, **then** it returns **HTTP 200** with `state: "down"` — never a non-2xx that forces the shell caller into an unparsable error branch.

### Edge Cases

- `_anon` and `api-key` pseudo-bearers carry `unified: null` — they must never be elected as "the account serving me".
- A quarantined credential (`_bearer_credential_dead`, `proxy.py:1160-1162`) is unusable but carries no windows and no Retry-After; it must not read as an idle zero-utilization winner.
- Both windows present with no `representative_claim` — `_binding_window` tie-breaks to `7d` only when strictly greater (`ratelimit.py:325-346`).
- Process restart: history is process-local, so `state_since_s` legitimately restarts at 0.
- Central tier (`CENTRAL_URL` set) — the local proxy holds no credentials; `account` degrades to nulls rather than lying.

## Requirements

### Functional Requirements

- **FR-001**: The proxy MUST expose `GET /__throttle/statusline` returning JSON, registered alongside the other infrastructure probes (`proxy.py:4322-4324`) and therefore **above** the catch-all route.
- **FR-002**: The endpoint MUST NOT consume a bearer slot, MUST NOT increment `served`, and MUST NOT be forwarded upstream — it is an infrastructure probe in the same class as `root_probe` and `health`.
- **FR-003**: The response MUST be ≤ **1024 bytes** and MUST NOT contain any collection whose length scales with client count, bearer count, or request history.
- **FR-004**: `account` MUST describe the bearer the hot path would select **now**, computed from the same ranking inputs as `_account_routing_candidate_score`, not a first/default/most-recent bearer.
- **FR-005**: `account.util`, `account.window`, and `account.status` MUST be derived via `_binding_utilization` / `_binding_window` applied to `unified_live_view(unified, now)` — never to the raw stored snapshot.
- **FR-006**: `account.stale` MUST be `true` when the RAW stored snapshot's binding window has a reset epoch already in the past at read time — i.e. when FR-005's live-view drops a window. It reports *"the proxy's knowledge of this account is aged; it has not been re-probed since its window rolled"*, so a renderer can mark the number as unconfirmed rather than trusting it. When every window is dropped, `window`/`util`/`status`/`reset` are `null` and `stale` is `true`. Consequence, and the point of the pairing: an emitted non-null `account.reset` is ALWAYS in the future.
- **FR-007**: The endpoint MUST accept an optional `?model=<id>` query parameter and, when present, apply the scoped per-model weekly meter to selection exactly as the hot path does.
- **FR-008**: `state` MUST be one of exactly `down | exhausted | throttled | queued | warn | ok`, resolved by **first match in that order, most severe first**. Normative resolution: `down` ⇐ `upstream_egress_ok` false; `exhausted` ⇐ `fleet.usable == 0`; `throttled` ⇐ selected bearer has `retry_after_until` in the future or a `rejected` binding window; `queued` ⇐ `queue.depth > 0`; `warn` ⇐ binding `status == allowed_warning` or `util >= THROTTLE_UTILIZATION_WARN` (live `brake.warn`, default 0.9); else `ok`.
- **FR-009**: The endpoint MUST return HTTP **200 in every state**, including upstream-egress failure (which health signals as 503). State is carried in the body, never in the status code.
- **FR-010**: The response MUST carry `Cache-Control: no-store` and MUST NOT be served from a stale cached body.
- **FR-011**: The endpoint MUST perform no upstream I/O, no blocking file read, and no per-request credential-file parse; account labels MUST come from the existing `(mtime_ns, size)`-keyed cache in `accounts.account_snapshot`.
- **FR-012**: Raw bearer tokens, credential paths, and account emails MUST NOT appear (invariant #2). Only the 8-hex `bearer_id` and the configured short label may be published.
- **FR-013**: `/__throttle/health` MUST remain byte-identical in schema — this feature adds a surface, it does not reshape the existing one.
- **FR-014**: The payload MUST carry `schema: "statusline/1"`; any future breaking field change requires a new version string rather than silent reshaping.

### Response shape (normative)

```json
{
  "schema": "statusline/1",
  "now": 1786933519,
  "state": "queued",
  "state_since_s": 754,
  "account": {
    "label": "C",
    "bearer": "666a53af",
    "window": "5h",
    "util": 0.25,
    "status": "allowed",
    "reset": 1786950600,
    "stale": false
  },
  "queue": { "depth": 23, "inflight": 10, "cap": 5 },
  "blocked_until": null,
  "fleet": { "usable": 2, "configured": 3 },
  "queue_mode": "fair"
}
```

**322 bytes compact** (`jq -c . | wc -c`) with realistic live values — 31 % of the 1 KB ceiling, leaving headroom for one future window field without a schema bump. 18 scalar leaves.

| Field | Type | Source | Answers |
|---|---|---|---|
| `schema` | `str` | constant | version pin (FR-014) |
| `now` | `int` | `time.time()` | lets the renderer compute *remaining* without clock-skew guesswork |
| `state` | `enum` | derived, FR-008 | "am I throttled or queued" |
| `state_since_s` | `int` | `history.level_since` | "how long has this held" — the `THROTTLED for 12m` signal |
| `account.label` | `str\|null` | `accounts.bearer_labels()` | **which account** (`A`/`B`/`C`) |
| `account.bearer` | `str\|null` | `_bearer_id` hash, 8 hex | joins to health/metrics for drill-down |
| `account.window` | `"5h"\|"7d"\|null` | `_binding_window` | **which** window binds |
| `account.util` | `float\|null` | `_binding_utilization` | **how full** |
| `account.status` | `str\|null` | live-viewed `status_{5h,7d}` | `allowed` / `allowed_warning` / `rejected` |
| `account.reset` | `int\|null` | live-viewed `reset_{5h,7d}` | **when does it reset** |
| `account.stale` | `bool` | reset epoch vs `now` | is this reading describing a dead window (FR-006) |
| `queue.depth` | `int` | `limiter.queued_total` | how deep the line is |
| `queue.inflight` | `int` | `limiter.inflight` | how many are moving |
| `queue.cap` | `int` | `limiter.max_concurrent` | live AIMD ceiling (shows the shrink) |
| `blocked_until` | `int\|null` | `limiter.retry_after_until` | hard pause epoch, `null` when unpaused |
| `fleet.usable` | `int` | `routing.bearer_usable` count | is there anywhere else to go |
| `fleet.configured` | `int` | `parse_spec(ACCOUNT_CRED_PATHS)` | denominator for the above |
| `queue_mode` | `str` | `config.QUEUE_MODE` | is admission even on (`off` ⇒ depth is meaningless) |

### Deliberate exclusions (vs. the health blob)

| Excluded | Bytes in live health | Why |
|---|---|---|
| `bearers[].clients` | **55,437** (1,006 entries) | Unbounded per-TCP-connection topology. Answers a fair-queue starvation question that is a *debugging* question, not a *per-render* one. This single key is **80 % of the whole blob** (91 % of `bearers`). |
| `limiter.rr_order`, `queued_per_client` | **886** at the sampled depth | Same topology, same reason; both scale with concurrent clients, violating FR-003. Small only because the queue was 23 deep at sample time. |
| `bearers[].last_ratelimit` | **1,442** | Raw upstream headers. The parsed `unified` block already carries every number a statusline reads; publishing both invites two sources of truth. |
| **Non-selected bearers** | — | A statusline renders one account. A fleet table is `/ui`'s job (invariant #6). Including all bearers reintroduces O(bearers) growth for a question about one. |
| `last_advisor` | var. | Unbounded GROQ prose — not glanceable, and its cost model is a dashboard concern. |
| `build`, `version`, `upstream`, `api_key`, `brake`, `account_identity`, `central_*`, `upstream_*_error` | **365** | Constant per process. These are deploy-verification and operator-config surfaces (the `running == persisted` drift check in CLAUDE.md); re-sending them 100×/s is pure waste. |
| Raw tokens / credential paths / emails | — | Invariant #2 — non-negotiable. |

The exclusions are the design. Health stays the operator's full-fidelity surface; the statusline endpoint is a *projection* of it, not a competitor. FR-013 keeps health unchanged so no existing consumer (`claude-account-pick`, the Dokku healthcheck, `/ui`) is disturbed.

### Latency and cost budget

Invariant #4 already binds `/__throttle/health` to **<50 ms** because Dokku polls it every 5 s with a 5 s timeout. This endpoint inherits that ceiling and tightens it, because its call rate is ~2 orders of magnitude higher (35 panes × ~3 renders/s vs. 0.2 req/s):

- **p95 < 50 ms** — inherited hard ceiling (invariant #4).
- **p95 strictly below health's measured p95** — health is 2.3–2.9 ms today; a projection that costs *more* than the blob it projects has failed its purpose.
- **O(1) in client count** — payload and CPU independent of the `clients` map (FR-003). This is what actually buys the budget: health's cost is dominated by serializing 1,006 client entries.
- **Zero blocking I/O on the request path** (FR-011). Health already walks all three configured credential paths per read via `_account_identity_verdict` → `account_snapshot` (one `os.stat` each) plus `guard_email` (mtime-gated, cached); at render cadence that becomes ~350 stat/s. The statusline path must hit the existing `(mtime_ns, size)`-keyed cache and MUST NOT add its own credential-file traversal.

## Success Criteria

### Measurable Outcomes — three falsifiable criteria

Each names the command whose output would **prove the criterion false**.

- **SC-001 — Bounded, O(1), correctly shaped.**
  The payload is ≤ 1024 bytes and its key set is exactly the normative shape, regardless of how many clients the process has tracked.

  ```sh
  # `-q` is MANDATORY and must be the FIRST argument: Pedro's ~/.curlrc sets
  # `continue-at -` (a second run against an existing -o target tries to RESUME
  # and reports size_download=0 — reproduced 16/08/2026), plus `compressed`
  # (which would measure wire bytes, not payload bytes, against the 1 KB bound)
  # and `retry = 3` (which would distort SC-002's latency samples).
  # FALSIFIED if size > 1024, or if the key-set diff prints anything.
  curl -q -fsS 'http://127.0.0.1:8765/__throttle/statusline' -o /tmp/sl.json
  # Measure the BODY, not the wire: immune to any future transport compression.
  test "$(wc -c < /tmp/sl.json)" -le 1024 || echo 'FALSIFIED: over 1KB'
  # NOTE: `paths(scalars)` is WRONG here — jq's `select` drops false and null,
  # so `account.stale:false` and `blocked_until:null` would silently vanish and
  # the check would pass on a payload missing them. Verified 16/08/2026.
  leaves() { jq -r '[paths as $p
    | select((getpath($p)|type)|.!="object" and .!="array") | $p|join(".")]|sort|.[]' "$1"; }
  diff <(leaves /tmp/sl.json) \
       <(printf '%s\n' schema now state state_since_s account.label account.bearer \
           account.window account.util account.status account.reset account.stale \
           queue.depth queue.inflight queue.cap blocked_until fleet.usable \
           fleet.configured queue_mode | sort) \
    || echo 'FALSIFIED: shape drift'
  # O(1) proof: tracked-client count must not move the payload size.
  curl -q -fsS http://127.0.0.1:8765/__throttle/health | jq '[.bearers[].clients//{}|length]|add'
  ```

- **SC-002 — Cheaper than the blob it replaces, and never a non-2xx.**
  Over 200 sequential requests, statusline p95 < 50 ms **and** < health p95; every response is HTTP 200 including while upstream egress is down.

  **Precondition on the comparative half:** the `< health p95` claim is judged ONLY when health is materially fat (≥ 4 KB). A cold instance with 0 bearers and 0 clients serves an 841-byte health body (measured 17/08/2026), at which point both endpoints collapse to the same ~1 ms per-request floor and the comparison is a coin flip, not a measurement. A projection can only be proven cheaper than the blob it projects when a blob exists. The absolute 50 ms ceiling (invariant #4) applies unconditionally.

  ```sh
  # FALSIFIED if sl_p95 >= health_p95, if sl_p95 >= 0.050, or if any code != 200.
  # `-q` also disables curlrc's `retry = 3` / `retry-delay = 2`, which would
  # otherwise fold retry sleeps into %{time_total} and poison the p95.
  for ep in statusline health; do
    for i in $(seq 200); do
      curl -q -fsS -o /dev/null -w '%{time_total} %{http_code}\n' \
        "http://127.0.0.1:8765/__throttle/$ep"
    done > "/tmp/t-$ep.txt"
    echo -n "$ep p95="; sort -n "/tmp/t-$ep.txt" | awk 'NR==190{print $1}'
    echo -n "$ep non200="; awk '$2!=200' "/tmp/t-$ep.txt" | wc -l
  done
  ```

  (Health legitimately returns 503 on egress failure — the non-200 check applies to `statusline` only, per FR-009.)

- **SC-003 — Never reports a dead window as live capacity.**
  The selected account's binding window is never presented with `stale:false` while its own reset epoch is in the past — the exact `47f0b262` trap measured above.

  ```sh
  # FALSIFIED if this jq exits 0 (i.e. it FOUND a past-reset window claimed fresh).
  curl -q -fsS 'http://127.0.0.1:8765/__throttle/statusline' \
    | jq -e --argjson now "$(date +%s)" \
        '.account.reset != null and .account.reset <= $now and .account.stale == false' \
    && echo 'FALSIFIED: stale window rendered as live'
  # Cross-check against health's RAW snapshot for the same bearer:
  b=$(curl -q -fsS http://127.0.0.1:8765/__throttle/statusline | jq -r .account.bearer)
  curl -q -fsS http://127.0.0.1:8765/__throttle/health | jq --arg b "$b" '.bearers[$b].unified, .bearers[$b].unified_at'
  ```

  Mutation check: neutering the `unified_live_view` call in the selection path MUST make this criterion fail against a fixture whose reset epoch has passed.

## Assumptions

- The consumer is a shell renderer with `curl` + `jq` already on PATH (both are in the statusline script's closure today, `claude-code.nix:1331+`). No new client dependency.
- Statusline integration in `~/NixOS` is a **separate change in a separate repo** — this spec defines the producer contract only. The consumer keeps its stdin-JSON path as a fallback when the proxy is unreachable.
- Render cadence is Claude Code's own; the endpoint does not attempt to impose one. If fan-out later exceeds what O(1) serialization absorbs, a short server-side TTL is the escalation — deliberately not specified now (YAGNI, and it would conflict with FR-010).
- Per-client attribution is explicitly **not** attempted: the statusline is a distinct process from its TUI and cannot share the TUI's TCP peer port, so `_client_id` (`ratelimit.py:57-80`) cannot join them. "Serving me" is therefore defined as *next-hop selection*, which is forward-looking and well-defined — and is what a renderer actually wants.
- Scope is this repo only. No implementation, no source change, no Nix change, no deployment mutation lands with this spec.

## Out of Scope

- Any edit to `src/anthropic_throttle_proxy/**` — this is a spec-only deliverable.
- Reshaping, shrinking, or paginating `/__throttle/health` (FR-013 forbids it here; the `clients` map's unbounded growth is a real but separate concern).
- Fleet/multi-proxy aggregation — `THROTTLE_FLEET_HEALTH` already covers the dashboard case.
- Prometheus changes; `/metrics` already exposes the unified gauges for time-series consumers.
- The `~/NixOS` statusline renderer change that will consume this endpoint.
