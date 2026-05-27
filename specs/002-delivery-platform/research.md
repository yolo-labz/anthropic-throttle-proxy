# Research: Throttle Proxy Delivery Platform

This document records the non-obvious decisions baked into the existing
codebase, so the plan is auditable end-to-end. No `NEEDS CLARIFICATION`
markers remain in the spec; this is closing the loop on rationale for
choices already implemented.

## Decision: aiohttp + raw `ClientSession` on the hot path, no vendor SDK

**Decision**: The proxy's request-forwarding hot path imports only
`aiohttp` (server + raw `ClientSession`) and `prometheus-client`. No
`anthropic`, `openai`, or `groq` SDK is reachable from `proxy.py`,
`forwarding.py`, `limiter.py`, or `pacing.py`.

**Rationale**:
- Vendor SDKs ship their own retry, backoff, and connection-pool logic.
  Composing them with an in-process AIMD throttle, unified-window pacing,
  and bearer fair queue would produce overlapping, hard-to-reason-about
  behavior under a 429 storm.
- A 429 storm against Anthropic must not also block the operator's
  ability to *diagnose* the storm. The advisor is therefore in a
  different module, lazy-imported, and talks to a deliberately
  independent provider (GROQ) over raw `aiohttp`.
- Supply-chain blast radius: a transitive bug in `anthropic` would
  otherwise reach the proxy. Keeping the hot path SDK-free shrinks the
  attack/bug surface to `aiohttp` + `prometheus-client`.

**Alternatives considered**:
- *Use the official `anthropic` SDK to talk to Anthropic.* Rejected: we
  are a transparent reverse proxy, not a client. We forward whatever
  bytes arrive, including headers we don't understand yet (`anthropic-
  ratelimit-unified-*` was new in 21/05/2026).
- *Use FastAPI for the server.* Rejected: FastAPI sits on top of
  Starlette + uvicorn; for a single-process catch-all reverse proxy with
  streaming response bodies, `aiohttp`'s native handler model is
  simpler.

## Decision: process-local `CollectorRegistry`, never the global default

**Decision**: `metrics.py` constructs its own `prometheus_client.CollectorRegistry`
and registers all counters/gauges against it. `/metrics` serves from this
registry, not from `prometheus_client.REGISTRY`.

**Rationale**:
- Even though the proxy runs single-worker today, any future
  uvicorn-style worker scaling that imports the metrics module twice in
  one process (e.g. via `multiprocessing.fork`) would double-register
  every metric against the global registry and crash on startup.
- Process-local registries make metric registration idempotent across
  worker patterns and make tests easier — each test can create a fresh
  registry without polluting global state.

**Alternatives considered**:
- *Use the global `REGISTRY`.* Rejected for the reasons above; the
  isolation cost is one extra line per metric definition.

## Decision: AIMD floor ≥ 1, never 0

**Decision**: `THROTTLE_AIMD_MIN` defaults to `1`. `FairBearerLimiter`
clamps the per-bearer live cap to `max(THROTTLE_AIMD_MIN, …)` on every
shrink. Configuration that tries to set it below `1` is treated as `1`.

**Rationale**:
- A floor of 0 would mean a bearer can be throttled to a complete stop,
  which would surface as a permanent hang to the IDE — indistinguishable
  from the proxy being down.
- The CUBIC-style multiplicative decrease (`THROTTLE_AIMD_DECREASE=0.7`)
  reaches 1 in ~5 steps from a ceiling of 8, which is fast enough to
  shed load but slow enough not to thrash.

**Alternatives considered**:
- *Allow floor = 0 with a separate "drain to zero" flag.* Rejected: the
  feature does not need this and the safety property is worth more than
  the flexibility.

## Decision: `529` is counted separately and does NOT shrink the cap

**Decision**: When upstream returns `529` (Anthropic-overloaded),
`anthropic_overload_total` increments but the bearer's cap is not
shrunk. The `529` response body is passed through to the client
unchanged.

**Rationale**:
- `529` is an upstream capacity event, not a "you sent too many
  requests" event. Misattributing it to client usage would cause the
  fleet to throttle itself for an Anthropic outage that has nothing to
  do with the fleet's traffic shape.
- `429` and `503` shrink the cap (you sent too many / upstream saturated
  from your traffic). `529` does not (Anthropic is just overloaded).

**Alternatives considered**:
- *Treat `529` like `503`.* Rejected: causes silent self-throttling
  during Anthropic outages.

## Decision: `Retry-After` is honored uncapped

**Decision**: `pacing.py` reads the `Retry-After` header from any
upstream response and refuses to dispatch the next request for the same
bearer until the window closes. There is no maximum.

**Rationale**:
- If Anthropic publishes `Retry-After: 600`, fighting it produces
  immediate `429`s and prolongs the storm. The proxy's job is to take
  the hint.
- Bearer growth (`grow()`) is also gated by the retry-after window: no
  growth happens until the window passes plus the cooldown.

**Alternatives considered**:
- *Cap retry-after at 60 s.* Rejected: when the storm is real, the cap
  just causes pain.

## Decision: Unified-window pacing reads utilization, not remaining counts

**Decision**: For OAuth bearers (Claude Code Max/Pro tokens), the proxy
parses `anthropic-ratelimit-unified-5h-{utilization,status,reset}` and
`anthropic-ratelimit-unified-7d-{utilization,status,reset}`, NOT the
API-key `remaining` family. Auto-pause when `status=rejected`; proactive
shrink when `THROTTLE_UTILIZATION_TARGET > 0` and a binding window
crosses the target.

**Rationale**:
- Measured 21/05/2026 on a Claude Code Max token: only the unified-*
  headers are present; the remaining-count family is empty for OAuth
  bearers. Parsing the remaining family for OAuth would silently no-op
  and miss the rate signal entirely.
- Utilization (0..1) is the canonical signal Anthropic publishes for
  OAuth. The `status` field flips to `rejected` when the bearer crosses
  the hard limit; the `reset` epoch is when the bearer comes back.

**Alternatives considered**:
- *Use the remaining-count family for both bearer types.* Rejected:
  empirically does not work for OAuth.

## Decision: Local admission cap when `queue_mode=off` and central is set

**Decision**: Even when `THROTTLE_QUEUE_MODE=off`, if
`THROTTLE_CENTRAL_URL` is set, the local proxy enforces a small fair
queue capped at `THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT` (default 2).
Same-host bursts beyond the cap are held until the cap clears.

**Rationale**:
- A same-host Claude Code burst (e.g. ten parallel sessions) could
  otherwise send 10 concurrent requests at central before central's AIMD
  feedback returns, defeating the global single-semaphore property.
- Cap of 2 is small enough to round-trip with central's feedback loop
  but large enough not to serialize all traffic.

**Alternatives considered**:
- *Cap at 1.* Rejected: serializes all same-host traffic and adds
  noticeable latency to the IDE.
- *Cap at the per-bearer ceiling.* Rejected: a single bearer can have
  many parallel client sessions; the cap is per-host, not per-bearer.

## Decision: GROQ as the advisor provider, raw `aiohttp` call

**Decision**: `ui/advisor_impl.py` calls GROQ's OpenAI-compatible
endpoint over raw `aiohttp`. No `groq` or `openai` SDK is imported. The
module is lazy-imported only when `ADVISOR_ENABLED=true` and the proxy
emits a `429`/`503`/`529` or the operator hits `POST /ui/advisor`.

**Rationale**:
- GROQ is a deliberately independent provider, so an Anthropic 429 storm
  does not also block the diagnosis.
- Raw `aiohttp` keeps the call surface identical to the proxy's own
  upstream calls — no transitive vendor dependency.
- Lazy import keeps the hot path free of even an unused vendor module.

**Alternatives considered**:
- *Use the `openai` SDK pointed at GROQ.* Rejected: violates
  constitution Principle I (no vendor SDK reachable from a code path
  that any throttle event can trigger).
- *Use the `groq` SDK.* Same reason.

## Decision: HTMX 1.x dashboard, one `<script>` tag, no ESM

**Decision**: `/ui` is a server-rendered Jinja2 template that loads HTMX
via a single `<script>` tag. No Alpine, no React, no ESM imports.
Catppuccin Mocha palette tokens only (no raw hex outside the tokens
file).

**Rationale**:
- One `<script>` tag is reviewable in a single tab; an ESM-importing
  page can pull dozens of transitive dependencies that drift between
  build runs.
- HTMX-driven partial swaps cover everything the dashboard needs
  (refresh stats, render advisor verdict). No client-side state machine
  needed.
- Catppuccin Mocha tokens come from a small CSS file
  (`ui/static/style.css`). Confining color to tokens means a theme
  change is one edit, not many.

**Alternatives considered**:
- *Use HTMX + Alpine for a couple of toggles.* Rejected: every JS dep
  is a supply-chain liability and the dashboard does not need it.

## Decision: Worktree-first repo policy

**Decision**: This repo follows the global mandatory worktree-first
rule. All in-repo feature work happens under `.worktrees/<branch>`;
`main` stays clean. `git stash` is forbidden.

**Rationale**:
- Pedro runs multiple Claude Code instances simultaneously against the
  same repo. Two agents editing one worktree corrupt each other's
  uncommitted edits, race on `git add`, and ship each other's WIP.
- Worktrees give each agent an isolated mutation surface that survives
  merge.

**Alternatives considered**:
- *Single shared worktree with feature branches.* Rejected: producing
  the failure modes above. The global CLAUDE.md documents the rationale
  in detail.

## Decision: Codex adversarial review mandatory before merge

**Decision**: Any throttle-path fix or claim that a throttle incident
is solved requires Codex adversarial review (`codex:codex-rescue` agent
or the `~/codex` CLI). Codex challenges causality, central/local
fallback behavior, limiter mode transitions, Nix pin hashes, and host
activation.

**Rationale**:
- The 25/05/2026 and 26/05/2026 incidents showed the proxy's failure
  modes are not local to the diff — they involve runtime symptom, code
  path, Nix pin, and host activation all aligning. A second
  adversarial reasoner catches misattributed fixes before they ship.

**Alternatives considered**:
- *Self-review.* Rejected by incident history.
- *Human-only review.* Rejected: Pedro is a one-person operator; a
  programmatic adversary that reads the diff + evidence end-to-end is
  cheaper than waiting on a human.
