# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Repo-local rules for Claude Code / opencode / codex sessions touching this codebase.

---

## Project context

Throttler is a self-hosted gateway and ingress for configurable provider lanes.
The default lane targets `api.anthropic.com`; the project originated from [anthropics/claude-code#53915](https://github.com/anthropics/claude-code/issues/53915). Lives standalone in `yolo-labz/` (was `~/NixOS/pkgs/anthropic-throttle-proxy/` until 20/05/2026 — see git history of `phsb5321/NixOS` for the rationale tree).

**Gateway roles (`throttler-gateway`), plus separate `throttler-ingress`:**
1. **local** — per-device proxy. Default `THROTTLE_QUEUE_MODE=off`. Optional fanout to a central instance via `THROTTLE_CENTRAL_URL`.
2. **central** — fleet-wide single semaphore. Runs on Dokku (new-install example `throttler.<your-host>`; existing app/domain names stay unchanged). `THROTTLE_QUEUE_MODE=fair`.


## Stack

- **Runtime**: Python 3.13+, `aiohttp` (server), `aiohttp-jinja2` (templates for HTMX UI), `prometheus-client` (metrics). No vendor AI SDK — the advisor talks to GROQ over raw `aiohttp` (see invariant #1).
- **Build**: `uv` for deps + venv. `hatchling` build backend. `pyproject.toml` is the single source of truth.
- **Deploy**: Dockerfile-based Dokku app. Multi-stage uv build per Astral's official pattern. No Heroku buildpacks.
- **Lint**: `ruff` (lint + format). Target Python 3.13. Line length 100.
- **Test**: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). `tests/` covers the proxy app, forwarding paths, pacing, unified-window parsing (~85% line coverage, gated in CI via SonarQube). New tests mirror `src/anthropic_throttle_proxy/` module layout.

## Architecture

Single-process aiohttp app, wired in `proxy.py::main()`. One line per part; the full bullets are in `docs/AGENT-RUNBOOK.md` ("Architecture (full bullets)"). Read them before you change that part.

- **Hot path** (`proxy.py::handler`) — catch-all `*` forwards to `THROTTLE_UPSTREAM` (or `THROTTLE_CENTRAL_URL`), streams back, parses SSE `usage`.
- **Per-bearer fair queue** (`FairBearerLimiter`) — round-robin per `client_id`; lazy per `bearer_id` under `bearer_limiter_lock`.
- **Bounded queue wait** — `THROTTLE_QUEUE_MAX_WAIT_S` (30 s) fails fast with a stamped 503; the bound is end-to-end across tiers.
- **Queue-depth admission** — refuses an arrival that cannot drain within the wait bound, only on measured evidence.
- **Priority lane** — `THROTTLE_PRIORITY_RESERVE_SLOTS` (default 0) dedicated pool for short calls.
- **AIMD reactive throttle** — ceiling shrinks on `429/503` (`THROTTLE_AIMD_DECREASE`), grows after `THROTTLE_AIMD_RAMP_AFTER`; `529` never shrinks.
- **Budget-vs-concurrency 429 split** — a `429` without `Retry-After` reads the unified headers: budget → full pause, concurrency → 2 s.
- **OAuth entitlement 429** — a headerless `rate_limit_error` 429 on `POST /v1/messages` is a request-shape refusal: relay it at once, no retry, no AIMD.
- **Synthetic probes must carry `CLAUDE_CODE_SYSTEM_PROMPT`** as the first `system` block on an OAuth token, or the gate above refuses them.
- **Header-aware pacing** — `_extract_ratelimit` honors `Retry-After` (uncapped) before the next dispatch.
- **OAuth unified-window pacing** — `_parse_unified`/`_apply_unified` track 5h/7d utilization; pause on `rejected`, warn at `THROTTLE_UTILIZATION_WARN`.
- **Burst pacing** — `_dispatch_lock` enforces `THROTTLE_MIN_DISPATCH_GAP_MS` between upstream POSTs.
- **Queue modes** (`THROTTLE_QUEUE_MODE`): `off`, `observe`, `fair`/`reactive`.
- **Central tier** — `THROTTLE_CENTRAL_URL` forwards to central; unhealthy central falls back to direct upstream.
- **Local account routing** — `THROTTLE_ACCOUNT_ROUTING=least_loaded` picks the least-loaded OAuth credential per `POST /v1/messages`.
- **Subscription-constrained ingress** (Spec 094) — `x-anthropic-throttle-require-credential-mode: subscription` fails closed with a pre-egress `403`.
- **Dashboard history** (`history.py`, `ui/signals.py`) — 360-slot, 10 s ring behind the sparklines.
- **UI** (`ui/routes.py::attach_ui`) — HTMX dashboard at `/ui`; `THROTTLE_UI_TOKEN_FILE` gates non-loopback peers.
- **Metrics** — process-local `CollectorRegistry` at `/metrics`; health JSON at `/__throttle/health`.

Distribution and entry: `throttler-gateway` (`python -m throttler_gateway`); ingress: `throttler-ingress`. Legacy commands, `anthropic_throttle_proxy` imports, wire contracts and state stay compatible.

## Load-bearing invariants

1. **The proxy hot path imports NO vendor AI SDK.** Hot path is aiohttp `Application` + raw `aiohttp.ClientSession` only. (The former GROQ "advisor" side-car was removed 27/09/2026 on Pedro's order — "it makes no sense" — so there is no second AI provider in the process at all now.) No transitive SDK bug can reach the proxy.
2. **Bearer token never logged.** `bearer_id` is `sha256(Authorization-header)[:8]` (`_bearer_id` in `proxy.py`) — only the hash appears in logs/metrics. `_anon` is used for unauthenticated requests (health/metrics) so they share one bypass slot.
3. **AIMD floor (`THROTTLE_AIMD_MIN`) is the safety net.** When upstream hits sustained 429s, live cap shrinks to floor; floor must stay ≥ 1 so traffic never fully blocks. Default 1.
4. **`/__throttle/health` must return in <50 ms.** Dokku healthcheck (`app.json`) polls it every 5 s with 5 s timeout. Anything that blocks the event loop here (sync I/O, large lock contention) breaks Dokku's restart policy.
5. **`THROTTLE_UPSTREAM` is the ONLY way to redirect traffic.** Never hard-code an upstream URL in source.
6. **The HTMX dashboard must render without JavaScript modules.** Pure HTMX 1.x (no Alpine, no React). One `<script>` tag for HTMX, server-rendered HTML. Catppuccin Mocha palette tokens only — no raw hex outside the tokens file.

## Don't break

- The `bearer_limiters` dict + `bearer_state` dict are read by Prometheus collectors; never mutate without holding `bearer_limiter_lock`.
- AIMD math (`shrink_on_pushback`, `ramp_on_success`) has a cooldown of `THROTTLE_AIMD_BACKOFF_S` (default 30 s) after each shrink before growth can resume — preserve the cooldown gate when refactoring.
- `prometheus_client` `CollectorRegistry` is process-local, not the default global one. Required because uvicorn-style workers would double-register metrics; we currently run single-worker but keep the registry isolated anyway.

## Runbook pointers (`docs/AGENT-RUNBOOK.md`)

- Before you change throttle behavior, central fallback, limiter state, pacing, Nix pins or deploy config: read "Incident workflow".
- Before a restart, or to learn what is running: read "Host service verification". Use `throttler-gateway.service` (`anthropic-throttle-proxy.service` is a symlink to it).
- After any Home Manager, Nix or systemd-user fix: run "Persistence checklist". For a stale-unit symptom: read "Stale-unit / root-probe incident".
- Before you write or edit a skill under `.claude/skills/`: read "Skill design notes".
- Before the first Dokku deploy: read "Deploy (one-time)".

A throttle-path change needs a test on the exact failure path, full pytest and ruff, green CI, and runtime verification after the deploy. A review by a different seat is advisory (fleet rule of 14/09/2026). Do it when a reviewer lane is available. An unavailable reviewer never blocks a merge.

## Where to work

The shared checkout stays on plain `main` and is read-only: read code there, update it with `git pull --ff-only`, never edit, commit or switch in it. Do the work in a fresh clone under `~/.cache/<workspace>/` (one clone per task, one branch, one PR), and delete the clone after the merge. No new worktrees. GitButler workspace mode is off here since 22/11/2026: `but pull` fails on the stale branch `1111-pi-load-generation`.

Never push to `main` directly. Merge with `fleet-merge`; delete the branch in a separate call.

## Project-local skills

Use `.claude/skills/`: `throttle-incident` (live throttle failures, 429/503/529 storms), `nix-user-service` (Home Manager activation, persistent-vs-runtime unit mismatch), `deploy-dokku` (central Dokku instance).

## Local dev quickstart

```sh
uv sync
uv run throttler-gateway                   # proxy :8765, dashboard /ui, metrics /metrics, health /__throttle/health
uv run pytest                                # full suite (proxy/forwarding/pacing/unified)
uv run pytest tests/test_pacing.py::test_yyy # single test
uv run ruff check src tests                  # lint
uv run ruff format src tests                 # format
```

Point clients at the proxy via `export ANTHROPIC_BASE_URL=http://127.0.0.1:8765` — claude-code / opencode / codex / Anthropic SDKs all honour it; the `Authorization: Bearer …` header passes through unchanged.

## Ownership

Every open PR / issue / branch is Pedro's responsibility — per `~/Documents/Code/CLAUDE.md`. No "another agent will handle it." Babysit CI green, rebase on stale, merge clean.

## License

MIT.

🤖 This file is co-authored by Claude Code.
