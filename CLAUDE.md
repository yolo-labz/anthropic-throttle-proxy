# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Repo-local rules for Claude Code / opencode / codex sessions touching this codebase.

---

## Project context

Self-hosted reverse-proxy in front of `api.anthropic.com`. Born from [anthropics/claude-code#53915](https://github.com/anthropics/claude-code/issues/53915). Lives standalone in `yolo-labz/` (was `~/NixOS/pkgs/anthropic-throttle-proxy/` until 20/05/2026 — see git history of `phsb5321/NixOS` for the rationale tree).

**Two roles, same binary:**
1. **local** — per-device proxy. Default `THROTTLE_QUEUE_MODE=off`. Optional fanout to a central instance via `THROTTLE_CENTRAL_URL`.
2. **central** — fleet-wide single semaphore. Runs on Dokku (`anthropic-throttle.<your-host>`). `THROTTLE_QUEUE_MODE=fair`.

## Stack

- **Runtime**: Python 3.13+, `aiohttp` (server + the advisor's GROQ call), `aiohttp-jinja2` (templates for HTMX UI), `prometheus-client` (metrics). No vendor AI SDK — the advisor talks to GROQ over raw `aiohttp` (see invariant #1).
- **Build**: `uv` for deps + venv. `hatchling` build backend. `pyproject.toml` is the single source of truth.
- **Deploy**: Dockerfile-based Dokku app. Multi-stage uv build per Astral's official pattern. No Heroku buildpacks.
- **Lint**: `ruff` (lint + format). Target Python 3.13. Line length 100.
- **Test**: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). `tests/` is empty at v0.1.0 — new tests mirror `src/anthropic_throttle_proxy/` module layout.

## Architecture

Single-process aiohttp app, wired in `proxy.py::main()`:

- **Hot path** (`proxy.py::handler`) — catch-all `*` route forwards every request to `THROTTLE_UPSTREAM` (or `THROTTLE_CENTRAL_URL` if set), streams response back, parses SSE `usage` blocks for token/cost metrics.
- **Per-bearer fair queue** (`FairBearerLimiter`) — replaces a flat `asyncio.Semaphore`. Lazy-allocated per `bearer_id` under `bearer_limiter_lock`. Round-robin across `client_id` (peer host:port or `X-Throttle-Client-Id` header) so one chatty TUI can't starve a sibling.
- **AIMD reactive throttle** — per-bearer ceiling shrinks on `429/503/529`, grows additively after `THROTTLE_AIMD_RAMP_AFTER` (default 10) successes past the `THROTTLE_AIMD_BACKOFF_S` (default 30 s) cooldown. Floor is `THROTTLE_AIMD_MIN` (default 1).
- **Burst pacing** — single process-global `_dispatch_lock` enforces `THROTTLE_MIN_DISPATCH_GAP_MS` between consecutive upstream POSTs. Orthogonal to `CLAUDE_API_THROTTLE_MAX` (which caps concurrency, not rate).
- **Queue modes** (`THROTTLE_QUEUE_MODE`): `off` (passthrough, no AIMD counters), `observe` (no queue but AIMD counters DO move — early-warning without slowdown), `fair`/`reactive` (queue + AIMD; `reactive` is an alias).
- **Central tier** — when `THROTTLE_CENTRAL_URL` is set, local proxy forwards there; background `central_health_loop` polls `/__throttle/health` every `THROTTLE_CENTRAL_HEALTH_INTERVAL`. Central unhealthy → transparent fallback to direct upstream.
- **UI** (`ui/routes.py::attach_ui`) — HTMX 1.x dashboard at `/ui`, jinja2 templates in `ui/templates/`. Advisor in `ui/advisor_impl.py` (`recommend()`): a cheap GROQ diagnosis of throttle events. Fires automatically (debounced) from `proxy._maybe_advise` on 429/503/529 and on demand via `POST /ui/advisor`; latest result lives in `state["last_advisor"]`. Gated by `ADVISOR_ENABLED` + `GROQ_API_KEY`.
- **Metrics** — `prometheus_client` with a process-local `CollectorRegistry` (NOT the default global), exposed at `/metrics`. Health JSON at `/__throttle/health` includes per-bearer `limiter.queued_per_client` for live starvation debugging.

Entry: `python -m anthropic_throttle_proxy` → `__main__.py` → `proxy.main()`. Dockerfile uses the same CMD.

## Load-bearing invariants

1. **The proxy hot path imports NO vendor AI SDK.** Hot path is aiohttp `Application` + raw `aiohttp.ClientSession` only. The advisor calls GROQ's OpenAI-compatible endpoint over raw `aiohttp` — a deliberately INDEPENDENT provider, so a 429 storm against Anthropic doesn't also block the diagnosis, and no transitive SDK bug can reach the proxy. It is lazy-imported only when `ADVISOR_ENABLED=true` and a throttle fires (or `/ui/advisor` is hit).
2. **Bearer token never logged.** `bearer_id` is `sha256(Authorization-header)[:8]` (`_bearer_id` in `proxy.py`) — only the hash appears in logs/metrics. `_anon` is used for unauthenticated requests (health/metrics) so they share one bypass slot.
3. **AIMD floor (`THROTTLE_AIMD_MIN`) is the safety net.** When upstream hits sustained 429s, live cap shrinks to floor; floor must stay ≥ 1 so traffic never fully blocks. Default 1.
4. **`/__throttle/health` must return in <50 ms.** Dokku healthcheck (`app.json`) polls it every 5 s with 5 s timeout. Anything that blocks the event loop here (sync I/O, large lock contention) breaks Dokku's restart policy.
5. **`THROTTLE_UPSTREAM` is the ONLY way to redirect traffic.** Never hard-code an upstream URL in source.
6. **The HTMX dashboard must render without JavaScript modules.** Pure HTMX 1.x (no Alpine, no React). One `<script>` tag for HTMX, server-rendered HTML. Catppuccin Mocha palette tokens only — no raw hex outside the tokens file.

## Don't break

- The `bearer_limiters` dict + `bearer_state` dict are read by Prometheus collectors; never mutate without holding `bearer_limiter_lock`.
- AIMD math (`shrink_on_pushback`, `ramp_on_success`) has a cooldown of `THROTTLE_AIMD_BACKOFF_S` (default 30 s) after each shrink before growth can resume — preserve the cooldown gate when refactoring.
- `prometheus_client` `CollectorRegistry` is process-local, not the default global one. Required because uvicorn-style workers would double-register metrics; we currently run single-worker but keep the registry isolated anyway.

## Local dev quickstart

```sh
uv sync
uv run python -m anthropic_throttle_proxy   # proxy :8765, dashboard /ui, metrics /metrics, health /__throttle/health
uv run pytest                                # tests (none yet at v0.1.0)
uv run pytest tests/test_xxx.py::test_yyy    # single test
uv run ruff check src tests                  # lint
uv run ruff format src tests                 # format
```

Point clients at the proxy via `export ANTHROPIC_BASE_URL=http://127.0.0.1:8765` — claude-code / opencode / codex / Anthropic SDKs all honour it; the `Authorization: Bearer …` header passes through unchanged.

## Deploy (one-time)

```sh
ssh dokku@your.host
dokku apps:create anthropic-throttle
dokku ports:add anthropic-throttle http:80:8765
dokku config:set anthropic-throttle \
  CLAUDE_API_THROTTLE_MAX=8 \
  THROTTLE_QUEUE_MODE=fair \
  THROTTLE_MIN_DISPATCH_GAP_MS=50
dokku checks:enable anthropic-throttle
git remote add dokku dokku@your.host:anthropic-throttle
git push dokku main
```

## Ownership

Every open PR / issue / branch is Pedro's responsibility — per `~/Documents/Code/CLAUDE.md`. No "another agent will handle it." Babysit CI green, rebase on stale, merge clean.

## License

MIT.

🤖 This file is co-authored by Claude Code.
