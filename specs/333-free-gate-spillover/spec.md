# Spec 333 — Cross-gate spillover for the OpenCode FREE gates (go ↔ zen)

**Status:** design draft (19/10/2026) for the 2026-10-19 free-models mission.
**Scope:** pure slice exploration — rules + guard tests only. NO src edits outside
this slice; no commit. Lead/integrator agent = pane w2P:p1H.

## 1. Problem (why this slice exists)

The fleet must run ALL OpenCode **free** models at maximum parallel width
WITHOUT ever triggering a rate limit:

- **zen gate** (`:8774` locally, upstream `https://opencode.ai/inference`)
  — free catalog: `mimo-v2.6-flash-free`, `longcat-2.5-preview-free`,
  `beam-free`, `deepseek-v4.1-flash-free`, `glm-5.3-flash-free`,
  `nemotron-*-free`, `space-bunny-free`.
- **go gate** (`:8770` locally, upstream `https://opencode.ai/inference/go`)
  — free catalog: `longcat-2.5-preview-free`.

Both gates are already fronted by the local throttler
(`opencode-{go,zen}-throttle-proxy` systemd user units, shared package
`throttler-gateway`). Client contract today: `queue_max_wait=180 s`,
`max_hold_retry_after=240 s`, go `AIMD_MIN=2` (bearer `e574f38a` re-registered
at 12 after the 19/10 incident).

The **live incident 19/10 ~18:57–19:06 on the go gate** shows what happens
when one gate saturates with nowhere else to go:

> budget-pushback 429 → `aimd-shrink` pinned shared bearer `e574f38a` to
> `max_concurrent=1` → with 171–303 s generations every other request got
> **pre-queue-depth 503** (`est_wait 210–355 s` > `max_wait 180 s`) → pi panes
> died in retry loops.

The two free gates are **isolated**: a saturated go cannot see zen headroom
and vice versa. This slice designs the **cross-gate spillover** so the fleet
keeps serving free traffic during a single-gate saturation — without ever
exceeding either gate's free budget.

## 2. Ground truth (receipts, all verified 19/10/2026 19:19–19:35 BRT)

### 2.1 Ingress (`src/anthropic_throttle_proxy/ingress.py`, :8760)

- `spill_request()` (ingress.py:1527) already handles **within-protocol**
  spillover: a lane answering `503 + x-anthropic-throttle-queue-timeout: 1`
  is detected by `_queue_timeout_503()` (ingress.py:1111), added to `tried`,
  and the next chain candidate is tried (`_spill_or_requeue`, ingress.py:1581).
- `spill_request()` **returns `None`** when every candidate is tried → caller
  `_handle_messages` answers the synthetic `503 + QUEUE_TIMEOUT_HEADER: "1"`
  after `GENERATE_RETRY_AFTER_S` (ingress.py:3830) — the exact client-facing
  shape that killed panes in the incident (retry loop into a full gate).
- The ingress is **Anthropic-protocol only** (`POST /v1/messages`, role
  inference from the model field, model-remap via `remap_body_model`). It is
  NOT an OpenAI-protocol router — the gates speak `/openai/*`.
- Live state: `:8760` listens on `127.0.0.1`; `throttler-ingress.nix` shows it
  is OFF-impact until clients are aimed at it.

### 2.2 Gate proxies (`opencode-throttle-proxy.nix` + `lib/throttle-proxy-instance.nix`)

- Two lanes, two semaphores, deliberately NOT shared:
  `go` = :8770 upstream `https://opencode.ai/inference/go`;
  `zen` = :8774 upstream `https://opencode.ai/inference`.
- `mkEnvAttrs` already supports every knob a cross-gate policy needs:
  `maxConcurrent`, `queueMode=fair`, `queueMaxWaitS`, `aimdMin`,
  `aimdInitialConcurrent`, `maxHoldRetryAfterS`, `stateRoot`,
  `extraEnv`. **No Nix-side change is required for the policy knobs** —
  they already flow to env.
- Live services: `opencode-go-throttle-proxy.service` and
  `opencode-zen-throttle-proxy.service` both `loaded active running`.
- Live go env: `THROTTLE_MAX_HOLD_RETRY_AFTER_S=240`, `THROTTLE_QUEUE_MAX_WAIT_S=180`,
  `THROTTLE_AIMD_MIN=2`, `CLAUDE_API_THROTTLE_MAX=12` (AIMD_INITIAL_CONCURRENT=12),
  and a late `CLAUDE_API_THROTTLE_MAX=1024` hard ceiling.

### 2.3 Live gate health (curl receipts, 19/10/2026)

```
:8770  upstream=https://opencode.ai/inference/go
       inflight=8  queued=0  served=257  max_concurrent=1024
       bearer e574f38a -> inflight=8  retry_after_until=1792448694 (future)
       admission_closed=false  queue_max_wait=<unset in JSON>
:8774  upstream=https://opencode.ai/inference
       inflight=0  queued=0  served=3
       bearer e574f38a -> retry_after_until=1792454401 (future)
       bearer _anon        -> retry_after_until=0.0
```

- The go gate is **NOW in post-incident shape**: `max_concurrent=1` AIMD floor
  has been re-raised to 12, bearer `e574f38a` is pinned at 8 inflight, and its
  `retry_after_until` is in the future — a **live budget hold**, not queue
  pressure. Traffic sent to go right now would wait out a 240 s hold or fail.
- The zen gate is **idle** (`inflight=0`) but its `retry_after_until` is ALSO
  in the future (1792454401 ≈ +95 min) — the SAME shared account's hold. **A
  spillover decision that only looks at `inflight` would send traffic into a
  held zen gate.** (The hold is per-account on the gateway side; the proxy
  surfaces it only through `retry_after_until` + `unified.*` status in the
  bearer view, not through a hard admission block.)

### 2.4 The 503 / retry-after contract (proxy.py)

- `_queue_wait_timeout_response` (proxy.py:769) answers `503` with
  `retry-after: <estimate>` + `x-anthropic-throttle-queue-timeout: "1"` and
  body "proxy queue wait exceeded; slots saturated — retrying will re-enter
  the fair queue". This is the signal both the ingress AND any cross-gate
  router must treat as **"this gate is full, try the other"**.
- `_is_queue_timeout_response` (proxy.py:766) detects the same stamp on
  **relayed** sibling responses.
- `health` (proxy.py:6269) exposes `inflight`, `queued`, `served`,
  `max_concurrent`, per-bearer `limiter.snapshot()` (incl. `retry_after_until`),
  `admission_closed`, `upstream_egress_ok`. **It does NOT expose
  `queue_max_wait`** in the JSON (verified: the field is absent from the
  live payload) — a router must NOT rely on reading it; it must read the
  `retry-after` on the 503 itself.

## 3. Design — cross-gate spillover rules

### 3.1 Where the router lives

**The cross-gate router is a THIRD role-aware tier, above the per-gate
proxies, and it is OpenAI-protocol.** Two viable placements, in order of
preference:

**Placement A (recommended): a new role-aware OpenAI ingress on a new port
(e.g. `:8776`).** Mirrors spec 093's proven shape: one role-tagged router in
front of the two gate throttles; per-gate proxies stay reachable as SPOF
fallback. The `:8760` ingress is Anthropic-protocol and cannot be reused
without a protocol fork — do NOT fork it.

**Placement B (lighter): a spill decision inside each gate proxy.** A
saturation-503 from go's upstream would be answered with a redirect/retry to
zen. Rejected: it leaks routing into the data plane, breaks the "each proxy is
a clean queue" invariant, and cannot make per-model-family opt-in decisions.

This spec designs for **A**; the rules are placement-agnostic and could be
unit-tested as a pure function against both placements.

### 3.2 Model-family map (the opt-in unit)

Spillover is gated **per model family**, never globally:

| family | default gate | may spill to | opt-in knob |
|---|---|---|---|
| `longcat-*` | **go** (it is the only free go model) | zen (longcat-2.5-preview-free is also in the zen catalog) | `free-spill-longcat` |
| `mimo-*` | zen | go (NO — go has no mimo) | n/a |
| `beam-*` | zen | go (NO) | n/a |
| `deepseek-*` | zen | go (NO) | n/a |
| `glm-*` | zen | go (NO) | n/a |
| `nemotron-*` | zen | go (NO) | n/a |
| `space-bunny` | zen | go (NO) | n/a |

**Rule R1 — asymmetry is the point.** Only `longcat-*` has a real spillover
pair (it is free on BOTH gates). Every zen-only free family is pinned to zen;
spilling it to go would 404 at the gateway (the go gateway does not carry the
zen catalog) — a **wasted round-trip + a 403/404 that looks like saturation**.
So the ONLY default-ON spill pair in practice is `longcat` go→zen, and it
stays OFF until R3's safety proofs hold.

### 3.3 Saturation signal (WHEN to spill)

Spill ONLY on a **queue-timeout 503** from the current gate — i.e. the exact
incident signal:

```
response.status == 503
  AND response.headers["x-anthropic-throttle-queue-timeout"] == "1"
```

**Rule R2 — NO spill on a raw 429.** A 429 is budget-pushback (AIMD's signal)
or an entitlement refusal — the gateway is telling us the ACCOUNT is limited,
and the shared account means both gates see it. Spilling a 429 would replay
the same doomed request against a sibling gate on the same account (the
Codex adversarial-review finding-4 hazard from ingress S5: "marking the lane
saturated would take a healthy lane out of rotation"). 429 → absorb and retry
in-lane (the existing `keepalive-hold` / AIMD path), never spill.

**Rule R3 — the headroom pre-flight (provably-safe gate).** Before spilling
family F from gate A to gate B, the router MUST re-probe B's health at
decision time and verify ALL of:

1. `inflight_B < max_concurrent_B` — a slot exists RIGHT NOW;
2. B's `retry_after_until <= now` — **no live budget hold** (the 19/10 zen
   receipt shows a hold can be invisible in `inflight`);
3. `admission_closed_B == false`;
4. B's own fresh 503 rate is not elevated (defence-in-depth: if B is also
   about to queue-timeout, spilling just doubles the client's wait — bound
   it with a per-family spill budget, e.g. ≤ N spills per 5 min).

If any check fails → **do NOT spill**; serve the 503 (the existing
retry-after contract) so the client retries within its 240 s hold window.

### 3.4 The spill execution

- **Model remap is unnecessary for longcat** (`longcat-2.5-preview-free` is
  the identical id on both gates — same catalog snapshot, verified in
  `lib/pi-opencode-models.nix` lines 918/1345). The router forwards the body
  byte-identical; only the `baseURL` changes.
- The router re-sends the request to gate B's `/openai/chat/completions`
  path, preserving query string. It stamps `x-anthropic-throttle-lane:
  <gate-B>` (reusing the ingress's MARKER vocabulary so dashboards can see
  the spill).
- **Budget invariant (the hard rule):** the router NEVER holds a slot on BOTH
  gates for one request. It releases A's 503'd attempt (the proxy already
  returned — A's queue slot is free) before dispatching to B, so the total
  concurrency against the shared account is `inflight_A + inflight_B ≤
  max_concurrent_A + max_concurrent_B` and each gate's own AIMD still governs
  its own upstream. The shared-account 429 risk is bounded by R3's hold
  check + the per-family spill budget.
- **One spill attempt per request.** If B also answers queue-timeout 503,
  the router serves the 503 (it does NOT ping-pong back to A — A was just
  proven saturated; that is the exact retry-loop that killed the panes).

### 3.5 Why this is provably budget-safe (the "unless provably safe" bar)

1. **Both free catalogs ride the same gateway account, not a paid metered
   lane.** Spilling free→free moves a request from one queue to another on
   the same zero-cost budget; the only shared resource is the account's
   concurrency/rate ceiling, which each gate's AIMD + the router's R3
   hold-check govern.
2. **The 429 path is never spilled.** The incident's root cause was a 429
   pushback; R2 ensures the router never amplifies it.
3. **Default OFF + per-family opt-in** means the mechanism cannot silently
   migrate traffic: `free-spill-longcat=false` (default) keeps today's
   behavior byte-for-byte (invariant: zero behavior change without opt-in).
4. **Bounded blast radius.** A wrong spill costs one wasted request to a gate
   that answers 404/403 on an unknown model — a client-visible error, not a
   budget breach, and it is rate-limited by R3.4's spill budget.

### 3.6 Client contract compatibility

- `queue_max_wait=180 s`: the R3 pre-flight is a health read (~ms), well
  inside the client's wait; the spilled request then queues on B under B's
  own 180 s bound.
- `max_hold_retry_after=240 s`: the served 503 carries the SAME retry-after
  contract, so a client that retries re-enters the same router (now seeing
  B's state fresh) — no pane-killing loop, because R3.4 caps spills and R1
  caps the ping-pong to one attempt.

## 4. Guard invariants (the tests that must pass before any opt-in flip)

These are the acceptance bar — each maps to a test in
`specs/333-free-gate-spillover/guard_tests/`:

- **G1 — default OFF.** With no opt-in knob set, a queue-timeout 503 from go
  is served VERBATIM (same 503, same retry-after) and zen receives ZERO
  requests. (Invariant: no behavior change.)
- **G2 — longcat opt-in spills on a queue-timeout 503.** With
  `free-spill-longcat=true`, go's queue-timeout 503 → request is re-sent to
  zen; zen's 200 is relayed with `x-anthropic-throttle-lane: zen`.
- **G3 — R2: a raw 429 from go is NEVER spilled**, even with the opt-in on.
  The client gets the 429 (or the existing in-lane retry), zen untouched.
- **G4 — R3 headroom gate blocks the spill.** With zen `inflight ==
  max_concurrent` (or `retry_after_until > now`, or `admission_closed`), the
  spill is refused and go's 503 is served verbatim. **This is the 19/10
  scenario: a saturated go must NOT push traffic into a held zen.**
- **G5 — no ping-pong.** If BOTH gates answer queue-timeout 503, the client
  gets ONE 503 and the router issues exactly ONE spill attempt.
- **G6 — no double-slot.** At no point does the router hold concurrency
  credit on both gates for the same request (assert via the gate proxies'
  `inflight` counters around the spill).
- **G7 — R3.4 spill budget.** After N spills per 5 min for a family, further
  queue-timeout 503s are served verbatim until the window clears.
- **G8 — zen-only families never spill.** A queue-timeout 503 from zen for
  `beam-free` is served verbatim even with the longcat opt-in ON (family map
  is per-model, not global).
- **G9 — non-free traffic never spills.** A paid/unknown model id is pinned
  to its configured gate; the free-spill path is reachable only for the
  free catalog ids (defence against a caller using the router to reach paid
  tiers).
- **G10 — 503 staleness bound.** The R3 pre-flight health read is fresh
  (<5 s old) and the request is dispatched within the client's 180 s
  wait; a stale health snapshot must NOT authorize a spill.

## 5. Implementation sketch (for the lead's integration decision)

A pure, unit-testable module `src/anthropic_throttle_proxy/free_gate_policy.py`:

```python
@dataclass(frozen=True)
class SpillDecision:
    action: Literal["serve", "spill", "hold"]
    target_gate: str | None = None
    reason: str = ""

class FreeGateSpillover:
    def __init__(self, families: dict[str, FamilyPolicy], budgets: SpillBudget): ...
    def decide(
        self,
        *,
        request_model: str,
        current_gate: str,
        current_response: aiohttp.ClientResponse,
        headroom: Mapping[str, GateHealth],   # fresh R3 pre-flight
        now: float,
    ) -> SpillDecision: ...
```

- `FamilyPolicy` encodes R1's map + per-family opt-in.
- `decide()` is a **pure function** — the guard tests exercise it directly
  with synthetic 503/429 responses and synthetic headroom, no network.
- The aiohttp server tier (the new `:8776` router) wraps `decide()` with the
  existing `_send_upstream` / `_is_queue_timeout_response` machinery from
  ingress.py (the stamp vocabulary is already shared).

**Nix side:** the router is a THIRD instance of the same
`opencode-throttle-proxy.nix` lane pattern (new unit
`opencode-free-spill-router`, new state root, new port). No new env knobs —
`mkEnvAttrs` already has `extraEnv`, `maxConcurrent`, `queueMaxWaitS`,
`maxHoldRetryAfterS`. The opt-in is a per-model-family map the router reads
at import (env JSON), defaulting to the OFF table in §3.2.

## 6. What this slice explicitly does NOT do

- No edits to `src/`, `proxy.py`, `ingress.py`, or the NixOS modules.
- No live traffic against the gates (the rules are validated by unit tests
  against the documented 503/health contracts, which are verified live).
- No change to AIMD, the 240 s hold, or the 180 s queue bound — those are
  already correct; the router only adds a SATURATION-triggered cross-gate
  path that respects them.

## 7. Handoff

- Lead/integrate agent (pane w2P:p1H): this spec + the guard tests are the
  deliverable. The router port choice (§3.1 A vs B) is the open decision;
  the spec recommends A (new `:8776` role-aware OpenAI ingress) because it
  reuses the proven ingress shape and keeps the per-gate proxies as the SPOF
  fallback.
