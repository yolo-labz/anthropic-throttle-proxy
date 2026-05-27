<!--
Sync Impact Report
==================
Version change: (none) → 1.0.0
Bump rationale: Initial ratification — five principles derived verbatim from
load-bearing invariants in CLAUDE.md (the operational guidance document that
predates this constitution).

Modified principles: (initial — none renamed)
Added sections:
  - Core Principles (I–V)
  - Engineering Constraints
  - Development Workflow & Incident Response
  - Governance
Removed sections: (none)

Templates requiring updates (existence checked, alignment verified):
  ✅ .specify/templates/plan-template.md — references "Constitution Check"
     gate, kept generic, no change required
  ✅ .specify/templates/spec-template.md — generic, no change required
  ✅ .specify/templates/tasks-template.md — generic, no change required
  ✅ .specify/templates/checklist-template.md — generic, no change required
  ✅ .specify/templates/constitution-template.md — this is the source template;
     deliberately left untouched

Runtime guidance docs:
  ✅ CLAUDE.md — already the canonical source; constitution derives from it
  ✅ .claude/skills/throttle-incident/SKILL.md — already references the
     `Incident workflow and adversarial review` section in CLAUDE.md
  ✅ .claude/skills/nix-user-service/SKILL.md — niri-guard activation gap
     documented
  ✅ .claude/skills/deploy-dokku/SKILL.md — central tier deploy+verify aligns
     with Principle V

Follow-up TODOs: (none)
-->

# anthropic-throttle-proxy Constitution

## Core Principles

### I. No Vendor AI SDK on the Hot Path (NON-NEGOTIABLE)

The request-forwarding hot path MUST import only `aiohttp` (server + raw
`ClientSession`) plus `prometheus-client`. No `anthropic`, `openai`, `groq`,
or any other vendor SDK may be imported by `proxy.py`, the limiter, the
pacing layer, or the central/health code paths. The HTMX advisor MAY call
GROQ over raw `aiohttp` and only when `ADVISOR_ENABLED=true`; it is
lazy-imported and lives under `ui/`.

Rationale: vendor SDKs ship retry, backoff, and connection-pool semantics
that conflict with the proxy's own AIMD throttle, unified-window pacing,
and bearer-fair queue. A 429 storm against Anthropic must not also
disable diagnostics. A transitive vendor bug must not reach the proxy.

### II. Bearer Identity is a Hash, Never a Secret (NON-NEGOTIABLE)

The proxy MUST identify bearers by `sha256(Authorization header)[:8]`
(`_bearer_id` in `proxy.py`) in every log line, metric label, dashboard
row, and health-endpoint field. Raw `Authorization` headers, raw API
keys, and raw OAuth tokens MUST NOT appear in logs, metrics, Prometheus
labels, journals, or HTMX-rendered output. Unauthenticated probes
(`/__throttle/health`, `/metrics`) share the constant bypass slot
`_anon`.

Rationale: this proxy sits between every client and Anthropic. A
journal leak, a metric scrape, or a screenshot of the dashboard must
not exfiltrate credentials.

### III. AIMD Floor is the Safety Net

`THROTTLE_AIMD_MIN` (default `1`) MUST stay `≥1`. The reactive throttle
SHALL shrink the per-bearer live cap toward this floor under sustained
`429`/`503` pushback (CUBIC-style, multiplier `THROTTLE_AIMD_DECREASE`,
default `0.7`) and ramp additively after `THROTTLE_AIMD_RAMP_AFTER`
(default `10`) successes past the `THROTTLE_AIMD_BACKOFF_S` (default
`30 s`) cooldown. `529` (upstream overloaded) MUST be counted separately
(`anthropic_overload_total`) and MUST NOT shrink the cap.

Rationale: the floor guarantees the proxy never fully blocks traffic.
A bug that lets the cap reach 0 would cause silent fleet-wide outages
that look like Anthropic problems. The `529` carve-out keeps Anthropic
capacity events from being misattributed to client usage.

### IV. Health Probes are Always Local and Cheap

`/__throttle/health`, `/metrics`, `GET /`, and `HEAD /` MUST be served
locally by the proxy without forwarding upstream or holding a bearer
queue slot. `/__throttle/health` MUST return JSON within 50 ms under
load to satisfy Dokku's 5 s healthcheck timeout. Any new code path
that blocks the event loop here (sync I/O, large lock contention,
upstream forwarding) is a regression.

Rationale: load balancers, Dokku healthchecks, and curl smoke tests
hit these endpoints. Forwarding them upstream wastes a bearer slot and
risks tripping Anthropic's rate limit on probe traffic. The
26/05/2026 stale-unit incident proved this: a regression that
forwarded `GET /` upstream silently broke probe responses on the
older build.

### V. Single Source of Truth for Upstream & Central Routing

`THROTTLE_UPSTREAM` is the ONLY way to redirect proxied traffic to a
non-default upstream. `THROTTLE_CENTRAL_URL`, when set, makes the
local tier forward to a central instance with transparent fallback to
direct upstream when the central health loop reports `down`. Hard-coded
upstream URLs, alternate environment-variable shims, or per-route
upstream overrides MUST NOT be introduced. The HTMX dashboard MUST
remain JavaScript-module-free (HTMX 1.x only, one `<script>` tag, no
Alpine, no React) and MUST use Catppuccin Mocha palette tokens — no
raw hex outside the tokens file.

Rationale: the proxy's reason to exist is one named upstream + one
named central. Adding redirection knobs creates configuration drift
across the fleet. The dashboard rule keeps `/ui` reviewable in a
single tab and immune to JS supply-chain attacks.

## Engineering Constraints

- Python ≥ 3.13. Lint and format with `ruff` (line length 100). Test
  with `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). Coverage
  gate ~85% via SonarQube `PROJECT_ANALYSIS_TOKEN` (never
  `USER_TOKEN`).
- Build with `uv` + `hatchling`. Deploy via Dokku Dockerfile (multi-stage
  uv build, no Heroku buildpacks).
- `prometheus_client` MUST use a process-local `CollectorRegistry`, never
  the default global one, to keep metric registration idempotent across
  uvicorn worker patterns even though the proxy currently runs
  single-worker.
- `bearer_limiters` and `bearer_state` are read by Prometheus collectors;
  mutation MUST hold `bearer_limiter_lock`.
- AIMD cooldown (`THROTTLE_AIMD_BACKOFF_S`, default 30 s) gates growth
  after a shrink. Preserve the gate when refactoring `shrink_on_pushback`
  / `ramp_on_success`.
- Header-aware pacing: `_extract_ratelimit` MUST capture
  `anthropic-ratelimit-*` and `Retry-After` into
  `bearer_state[bid]["last_ratelimit"]`. `FairBearerLimiter` MUST honor
  `Retry-After` (uncapped) and block `grow()` until the window closes.
- OAuth unified-window pacing: parse `anthropic-ratelimit-unified-{5h,7d}`
  utilization+status+reset into `bearer_state[bid]["unified"]`. Auto-pause
  on `rejected`; proactively shrink when `THROTTLE_UTILIZATION_TARGET>0`
  (default 0/off) and the binding window crosses the target.

## Development Workflow & Incident Response

- **Worktree-first**: edits MUST happen in `.worktrees/<branch>` (or a
  sibling worktree for cross-repo coordination). The main worktree
  stays on `main`, clean. `git stash` is forbidden — open another
  worktree.
- **Conventional commits**: `feat:` / `fix:` / `refactor:` / `chore:` /
  `docs:` subject ≤ 72 chars, `Co-Authored-By` trailer.
- **Mandatory PR flow**: feature branch → rebase on `origin/main` →
  push → `gh pr create` → wait for CI green → address review →
  `gh pr merge --squash --delete-branch`. Never push directly to `main`.
- **Codex adversarial review (MANDATORY)** before merging any
  throttle-path fix or declaring a throttle incident solved. Provide
  Codex: symptom, hypothesis, live evidence, diff, tests, deployment
  plan. Codex MUST challenge causality, central/local fallback
  behavior, limiter mode transitions, Nix pin hashes, and whether the
  activated host runs the new store path.
- **Evidence first**: before changing throttle behavior, central
  fallback, limiter state, request pacing, Nix pins, or deployment
  config — capture local + central `/__throttle/health`,
  `journalctl --user -u anthropic-throttle-proxy`, the user unit
  `ExecStart`, and the current Nix store path. State hypothesis and
  falsifier before editing.
- **Verify the deployed runtime**, not just the merged PR. Persistence
  is fixed only after the persistent symlink, daemon-reload, restart,
  and live `/__throttle/health` all agree on the new store path.
- **Credentials via `rbw` first**. `GROQ_API_KEY`, Dokku tokens, and
  any TLS material come from `rbw get` piped directly — never pasted
  into shell history.
- **Tests are not optional**. New tests MUST mirror
  `src/anthropic_throttle_proxy/` module layout. The full suite plus
  `ruff check` MUST pass before merge.

## Governance

This constitution supersedes all other practices in the
`anthropic-throttle-proxy` repository. CLAUDE.md remains the
operational guidance document (commands, recipes, incident logs);
this constitution captures the non-negotiable principles CLAUDE.md
must remain compatible with.

**Amendment procedure**: amendments propose a version bump (semver),
update this file via PR, and propagate changes to dependent Speckit
templates (`plan-template.md`, `spec-template.md`,
`tasks-template.md`, `checklist-template.md`) in the same PR. Codex
adversarial review is required on any amendment that relaxes a
principle marked NON-NEGOTIABLE.

**Versioning policy**:
- MAJOR: backward-incompatible principle removal or redefinition,
  including downgrading a NON-NEGOTIABLE principle.
- MINOR: new principle or section added; materially expanded
  guidance.
- PATCH: clarifications, wording, typo fixes.

**Compliance review**: every PR description MUST cite which principles
are affected. CI MUST gate merges on `ruff check`, full `pytest`, and
the SonarQube coverage threshold. Live-runtime verification (the
Persistence checklist in CLAUDE.md) MUST run before declaring any
host-facing fix complete.

**Version**: 1.0.0 | **Ratified**: 2026-05-26 | **Last Amended**: 2026-05-26
