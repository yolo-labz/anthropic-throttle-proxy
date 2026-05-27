# Feature Specification: Throttle Proxy Delivery Platform

**Feature Branch**: `002-delivery-platform`
**Created**: 2026-05-26
**Status**: Draft
**Input**: User description: "Complete delivery of anthropic-throttle-proxy: local root probes, systemd persistence verification, central Dokku health, queue and central fallback behavior, advisor/GROQ diagnosis, tests, docs, and PR readiness"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Developer survives an Anthropic rate-limit storm without dropped sessions (Priority: P1)

A developer runs `claude-code`, `opencode`, `codex`, or any Anthropic SDK against
`http://127.0.0.1:8765`. Anthropic's API begins returning `429` and `503`
responses during a busy editing session. The developer's session continues to
function: requests are paced, queued, and retried so the IDE never surfaces a
hard rate-limit error to the human.

**Why this priority**: This is the proxy's reason to exist. Without it, every
other story is decoration. A developer who sees a hard `429` from the proxy
gets the same broken experience as if the proxy were not there.

**Independent Test**: Point `claude-code` at the proxy, force a rate-limit
condition (parallel sessions, large prompts), and observe that the IDE
continues to complete requests with backoff while upstream returns `429`. No
operator intervention required.

**Acceptance Scenarios**:

1. **Given** a per-bearer ceiling of 8 in-flight requests, **When** upstream
   returns `429` on three consecutive responses for that bearer, **Then** the
   ceiling shrinks toward the floor and growth is paused until the cooldown
   elapses.
2. **Given** upstream returns `Retry-After: 5`, **When** the next request for
   the same bearer arrives, **Then** the proxy waits the full window before
   dispatch and does not grow the ceiling during that window.
3. **Given** an OAuth bearer whose unified 5-hour window returns
   `status=rejected` with a reset epoch, **When** the next request from that
   bearer arrives, **Then** the proxy holds it until the reset and does not
   forward to Anthropic.
4. **Given** upstream returns `529` (overloaded), **When** the proxy receives
   it, **Then** the per-bearer ceiling does NOT shrink, the `529` is counted
   separately, and the response is returned to the client.

---

### User Story 2 - Operator runs one central tier and every local host fans out through it with transparent fallback (Priority: P2)

The operator deploys a single central instance on Dokku
(`anthropic-throttle.<host>`). Every developer host runs a local proxy with
`THROTTLE_CENTRAL_URL` pointing at the central. While central is healthy, every
upstream request flows through it and the global concurrency cap is enforced
once. When central goes down, every local proxy falls back to forwarding
directly to Anthropic so developer sessions continue to work.

**Why this priority**: Multi-host coordination is the difference between a
toy local throttle and a fleet-safe one. Fallback is what makes the central
tier deployable without becoming a single point of failure.

**Independent Test**: Start a local proxy with a configured central URL.
Confirm `/__throttle/health` reports `central_status=up` and traffic reaches
central. Stop the central container. Within the configured health interval,
the local proxy reports `central_status=down` and continues to serve client
traffic by talking to Anthropic directly. Restart central; local recovers
without restart.

**Acceptance Scenarios**:

1. **Given** central is healthy and reachable, **When** a client sends a
   request to a local proxy with `THROTTLE_CENTRAL_URL` set, **Then** the
   request is forwarded to central, central enforces the single concurrency
   cap, and the response is streamed back to the client.
2. **Given** central is unreachable for longer than the health interval,
   **When** a client sends a request, **Then** the local proxy forwards
   directly to Anthropic and `/__throttle/health` reports
   `central_status=down`.
3. **Given** the local proxy is in `queue_mode=off` with a configured central,
   **When** the same host bursts more than `THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT`
   concurrent requests, **Then** the local admission cap holds the excess
   until the cap clears, preventing local bypass of central admission.

---

### User Story 3 - Health probes and a dashboard make the live throttle state observable without leaking secrets (Priority: P2)

Load balancers, Dokku healthchecks, Prometheus scrapers, and the operator's
dashboard all need an honest view of the proxy. Healthchecks must answer
locally and quickly. The dashboard must show per-bearer state, recent
throttle events, and the GROQ advisor's diagnosis of what just happened — all
without ever surfacing a raw bearer token or API key.

**Why this priority**: Observability is what turns "the proxy is working" from
faith into evidence. The advisor in particular turns a 4 AM throttle storm
into a one-paragraph explanation instead of a stack trace.

**Independent Test**: Curl `/__throttle/health`, `/metrics`, `GET /`, and
`HEAD /` against the local and central proxies. All four return within 50 ms.
Open `/ui`. Verify per-bearer rows show only 8-character hash IDs, never raw
tokens. Trigger a `429`. Verify the dashboard surfaces an advisor diagnosis
within the debounce window when the advisor is enabled.

**Acceptance Scenarios**:

1. **Given** the proxy is under load, **When** `/__throttle/health` is
   requested, **Then** it returns JSON within 50 ms without forwarding upstream
   and without holding a bearer queue slot.
2. **Given** a client sends `GET /` or `HEAD /`, **When** the proxy receives
   it, **Then** the proxy responds locally with a 200 and does NOT forward to
   `THROTTLE_UPSTREAM`.
3. **Given** any request is processed, **When** the bearer is logged or
   labeled in metrics, **Then** only the 8-character SHA-256 prefix of the
   `Authorization` header appears; the raw token never appears in logs,
   metric labels, the dashboard, or health JSON.
4. **Given** the advisor is enabled and `GROQ_API_KEY` is set, **When** the
   proxy emits a `429`, `503`, or `529`, **Then** the proxy calls GROQ
   (debounced) and surfaces the diagnosis at `/ui` and via
   `state["last_advisor"]`. When the advisor is disabled, no GROQ call is
   made and no vendor SDK is imported.

---

### User Story 4 - Operator reboots a host without silently regressing the proxy to an older build (Priority: P3)

After a Nix system rebuild or Home Manager activation, the operator reboots
the desktop host. After the reboot, the user systemd service runs the same
build that was tested before reboot — not an older Nix store path left behind
by a deferred activation.

**Why this priority**: This story exists because of the 26/05/2026 stale-unit
incident. A merged PR + green CI + bumped Nix pin proved the artifact was
correct, but the host was still running the old binary because the
`niri-guard` wrapper rewrites `nh os switch` to `nh os boot` and defers Home
Manager activation. Without persistence verification, reboot regresses the
service silently.

**Independent Test**: Apply a Nix change, run the persistence checklist (cat
vs show, surgical symlink swap if needed, daemon-reload, restart, re-verify),
reboot the host, and confirm `systemctl --user show ... -p ExecStart` after
the reboot matches the expected store path.

**Acceptance Scenarios**:

1. **Given** the persistent symlink at `~/.config/systemd/user/<unit>` points
   at the canonical Home Manager-files derivation, **When** the host reboots,
   **Then** the service starts with the expected `ExecStart` and the live
   `/__throttle/health` reports a build that includes the latest committed
   code paths.
2. **Given** a runtime drop-in under `/run/user/<uid>/systemd/user/...`
   currently pins the correct binary but a stale persistent symlink points at
   the previous one, **When** the operator runs `systemctl --user cat`, **Then**
   the discrepancy is visible without needing to reboot, allowing the
   surgical symlink swap before reboot regresses the service.

---

### Edge Cases

- **Anthropic capacity event (`529`)**: the per-bearer cap MUST NOT shrink;
  the proxy MUST count `529` separately and return it to the client. Treating
  it as a client-usage signal would cause the fleet to throttle itself for an
  Anthropic outage.
- **`Retry-After` larger than the cooldown**: the proxy MUST honor the
  uncapped `Retry-After` before the next dispatch and MUST block ceiling
  growth for the same bearer until the window closes.
- **OAuth unified-window `rejected` state**: the proxy MUST auto-pause that
  bearer until the published reset epoch. If `THROTTLE_UTILIZATION_TARGET > 0`
  and a window crosses the target, the proxy MUST proactively shrink that
  bearer's cap before Anthropic begins to push back.
- **Central flapping**: the central health loop polls every
  `THROTTLE_CENTRAL_HEALTH_INTERVAL`. Local fallback MUST remain stable
  across one or two unhealthy polls; sustained unhealthy state moves to
  direct-upstream forwarding without dropping in-flight requests.
- **Bearer queue with one chatty client**: the per-bearer fair queue MUST
  round-robin across `client_id` so a single chatty TUI cannot starve sibling
  clients sharing the same bearer.
- **Same-host burst with `queue_mode=off` and central configured**: the local
  admission cap (`THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT`, default 2) MUST
  hold the excess so the burst does not bypass local admission before
  central / AIMD feedback can return.
- **Probe traffic from load balancers**: `GET /`, `HEAD /`,
  `/__throttle/health`, and `/metrics` MUST be answered locally without
  consuming a bearer slot or forwarding upstream.
- **Reboot after a deferred Home Manager activation**: the persistence
  checklist (cat vs show, surgical symlink swap) MUST catch the stale unit
  before reboot regresses the service.

## Requirements *(mandatory)*

### Functional Requirements

#### Forwarding and routing
- **FR-001**: The proxy MUST forward every non-probe HTTP request to a single
  configured upstream (`THROTTLE_UPSTREAM`) and MUST NOT support per-route
  upstream overrides or alternate redirect mechanisms.
- **FR-002**: When `THROTTLE_CENTRAL_URL` is set, the proxy MUST forward
  upstream-bound traffic to the central instance, MUST poll central's
  `/__throttle/health` on a configurable interval, and MUST fall back to
  direct upstream forwarding when central reports unhealthy.
- **FR-003**: The proxy MUST stream upstream responses to the client without
  buffering the full body in memory.

#### Throttle and pacing
- **FR-004**: The proxy MUST maintain a per-bearer ceiling and shrink it
  multiplicatively on sustained `429`/`503` pushback, with a configurable
  decrease factor and a configurable cooldown gating subsequent growth.
- **FR-005**: The proxy MUST count `529` (upstream overloaded) separately and
  MUST NOT shrink the per-bearer ceiling on `529` responses.
- **FR-006**: The proxy MUST honor `Retry-After` headers (uncapped) before
  the next dispatch for the same bearer and MUST block ceiling growth until
  the window closes.
- **FR-007**: The proxy MUST parse `anthropic-ratelimit-unified-*` headers
  (5h/7d utilization, status, reset epoch) into per-bearer state and MUST
  auto-pause bearers whose unified window reports `rejected` until reset.
- **FR-008**: When `THROTTLE_UTILIZATION_TARGET > 0` and a binding unified
  window crosses the target, the proxy MUST proactively shrink that bearer's
  cap before Anthropic begins to push back.
- **FR-009**: The proxy MUST enforce a configurable minimum gap between
  consecutive upstream dispatches across all bearers (burst pacing) using a
  process-global dispatch lock.
- **FR-010**: The per-bearer queue MUST round-robin across `client_id` (peer
  host:port or `X-Throttle-Client-Id` header) so a single chatty client cannot
  starve sibling clients sharing the same bearer.
- **FR-011**: When `THROTTLE_QUEUE_MODE=off` and `THROTTLE_CENTRAL_URL` is
  set, the proxy MUST still enforce a configurable local admission cap
  (`THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT`) so same-host bursts do not bypass
  local admission before central feedback returns.

#### Health, probes, and observability
- **FR-012**: The proxy MUST answer `GET /`, `HEAD /`,
  `/__throttle/health`, and `/metrics` locally without forwarding upstream
  and without consuming a bearer queue slot.
- **FR-013**: `/__throttle/health` MUST return JSON within 50 ms under load
  and MUST include `queue_mode`, `inflight`, `queued`, `served`,
  `central_status`, and per-bearer queued-per-client counters.
- **FR-014**: The proxy MUST expose Prometheus metrics at `/metrics` using a
  process-local collector registry (not the global default).

#### Security and identity
- **FR-015**: The proxy MUST identify bearers by an 8-character SHA-256
  prefix of the `Authorization` header and MUST NOT log, label, render, or
  otherwise surface raw `Authorization` headers, raw API keys, or raw OAuth
  tokens anywhere (logs, metrics, dashboard, health JSON, journal).
- **FR-016**: Unauthenticated probes (`/__throttle/health`, `/metrics`,
  `GET /`, `HEAD /`) MUST share a single constant bypass slot identifier.

#### Dashboard and advisor
- **FR-017**: The proxy MUST serve an HTMX 1.x dashboard at `/ui` that
  renders without JavaScript modules (one `<script>` tag for HTMX, no Alpine,
  no React, no ESM imports) using Catppuccin Mocha palette tokens only — no
  raw hex outside the tokens file.
- **FR-018**: The advisor MUST be gated by `ADVISOR_ENABLED=true` AND a
  present `GROQ_API_KEY`. When enabled, the proxy MUST call GROQ's
  OpenAI-compatible endpoint (over raw HTTP — no vendor SDK) automatically
  (debounced) on `429`, `503`, and `529` events and on demand via
  `POST /ui/advisor`. The latest result MUST be exposed via
  `state["last_advisor"]`.
- **FR-019**: The advisor module MUST be lazy-imported only when triggered.
  It MUST NOT be imported by the proxy hot path under any code path.

#### Build, test, and deploy
- **FR-020**: The codebase MUST build with `uv` and `hatchling` from a
  single `pyproject.toml`. The Docker image MUST be Dockerfile-based,
  multi-stage, and follow Astral's official uv pattern (no Heroku
  buildpacks).
- **FR-021**: The test suite MUST cover the proxy app, forwarding paths,
  pacing (header-aware and burst), unified-window parsing, central fallback,
  root probes, and the advisor, with line coverage at or above ~85% gated
  in CI via SonarQube `PROJECT_ANALYSIS_TOKEN` (never `USER_TOKEN`).
- **FR-022**: The lint suite MUST be `ruff` (lint and format), targeting
  Python 3.13 with line length 100. CI MUST gate merges on `ruff check`
  passing.
- **FR-023**: Documentation MUST cover local dev quickstart, Dokku deploy,
  systemd persistence verification (including the niri-guard activation
  gap workaround), and central/local fallback semantics. `CLAUDE.md`,
  `README.md`, `docs/DEPLOY-DOKKU.md`, and the three project-local skills
  (`throttle-incident`, `nix-user-service`, `deploy-dokku`) MUST agree on
  invariants and procedures.

#### Persistence and runtime verification
- **FR-024**: Operators MUST be able to verify persisted unit state
  (`systemctl --user cat`) and effective runtime state
  (`systemctl --user show ... -p ExecStart`) and detect divergence before
  reboot regresses the service.
- **FR-025**: The persistence checklist in `CLAUDE.md` MUST yield a
  reproducible surgical fix when Home Manager activation is deferred (the
  niri-guard activation gap), with no reboot required.

### Key Entities *(include if feature involves data)*

- **Bearer**: A logical identity derived from the client's `Authorization`
  header. Identified externally by an 8-character SHA-256 prefix
  (`bearer_id`). Holds AIMD ceiling state, last seen rate-limit headers,
  unified-window utilization/status/reset, retry-after deadline, and
  per-client queue depth.
- **Client**: A peer of a bearer. Identified by peer host:port or a
  client-supplied `X-Throttle-Client-Id` header. Used by the per-bearer
  fair queue to round-robin across siblings sharing one bearer.
- **Central instance**: A single external proxy at `THROTTLE_CENTRAL_URL`
  that enforces the fleet-wide concurrency cap. Reachability is determined
  by a background health loop polling `/__throttle/health`.
- **Advisor verdict**: A GROQ-generated diagnosis of recent throttle
  events. Stored in `state["last_advisor"]` and rendered at `/ui`. Never
  contains raw bearer tokens.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A developer's `claude-code` session continues to complete
  requests through the proxy while upstream returns a sustained `429` storm.
  Zero hard rate-limit errors surface to the IDE during the storm.
- **SC-002**: When the central tier becomes unreachable, every local proxy
  reports `central_status=down` within one health interval and continues
  serving client traffic by forwarding directly to upstream. Client-visible
  request failures during the transition are zero for already-in-flight
  requests and below 1% for new requests during the first health interval.
- **SC-003**: 100% of probe traffic (`GET /`, `HEAD /`, `/__throttle/health`,
  `/metrics`) is answered locally with a 200, never forwarded upstream, and
  never consumes a bearer queue slot.
- **SC-004**: `/__throttle/health` returns JSON in under 50 ms in the 99th
  percentile under sustained load equal to the configured per-bearer
  ceiling.
- **SC-005**: Zero raw bearer tokens, raw API keys, or raw OAuth tokens
  appear in any operator-visible surface (logs, journals, Prometheus metric
  labels, HTMX dashboard, `/__throttle/health` JSON, GROQ advisor payloads).
- **SC-006**: A reboot of the desktop host does not regress the running
  binary path; `systemctl --user show ... -p ExecStart` after reboot
  matches the expected Nix store path that was active before reboot.
- **SC-007**: The full test suite + `ruff check` pass on every PR. Line
  coverage stays at or above ~85% as reported by SonarQube.
- **SC-008**: `/__throttle/health` reports `central_status=up` continuously
  while central is healthy and transitions to `down` within one health
  interval after central becomes unreachable, in both directions.

## Assumptions

- The proxy runs in a trusted network position: a developer's localhost
  (`http://127.0.0.1:8765`) and a single Dokku app accessible over HTTPS at
  `anthropic-throttle.<host>`. There is no per-request authentication of
  callers; the bearer header is passed through unchanged to Anthropic.
- Clients honor `ANTHROPIC_BASE_URL` and pass `Authorization` unchanged.
  `claude-code`, `opencode`, `codex`, and Anthropic's SDKs all behave this
  way today.
- The host running the local desktop service is a Nix/Home Manager system
  whose activation may be deferred (e.g. niri-guard rewriting `nh os switch`
  to `nh os boot`). The persistence checklist accommodates this.
- The central tier runs single-worker (single Python process) on Dokku.
  Multi-worker scaling is out of scope for this delivery.
- The HTMX dashboard targets evergreen browsers (Chromium-family,
  Firefox-current, WebKit-current). No legacy IE support.
- GROQ's OpenAI-compatible endpoint is the advisor provider. Switching to
  another OpenAI-compatible provider is configuration, not code change, as
  long as it remains accessible over raw `aiohttp`.
- Secret material (`GROQ_API_KEY`, Dokku SSH keys, TLS) lives in
  Bitwarden under the `api/<service>` convention and is fetched via `rbw`
  for any deploy or rotation; secrets never pass through shell history.
- The codebase is Python 3.13+ with `uv` as the dependency manager and
  `hatchling` as the build backend. No `pip` workflows or Heroku buildpacks
  are supported.
