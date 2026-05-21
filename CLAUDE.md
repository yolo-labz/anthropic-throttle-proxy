# CLAUDE.md — anthropic-throttle-proxy

Repo-local rules for Claude Code / opencode / codex sessions touching this codebase.

---

## Project context

Self-hosted reverse-proxy in front of `api.anthropic.com`. Born from [anthropics/claude-code#53915](https://github.com/anthropics/claude-code/issues/53915). Lives standalone in `yolo-labz/` (was `~/NixOS/pkgs/anthropic-throttle-proxy/` until 20/05/2026 — see git history of `phsb5321/NixOS` for the rationale tree).

**Two roles, same binary:**
1. **local** — per-device proxy. Default `THROTTLE_QUEUE_MODE=off`. Optional fanout to a central instance via `THROTTLE_CENTRAL_URL`.
2. **central** — fleet-wide single semaphore. Runs on Dokku (`anthropic-throttle.<your-host>`). `THROTTLE_QUEUE_MODE=fair`.

## Stack

- **Runtime**: Python 3.13+, `aiohttp` (server), `aiohttp-jinja2` (templates for HTMX UI), `prometheus-client` (metrics), `anthropic` (advisor only — never on the hot path).
- **Build**: `uv` for deps + venv. `hatchling` build backend. `pyproject.toml` is the single source of truth.
- **Deploy**: Dockerfile-based Dokku app. Multi-stage uv build per Astral's official pattern. No Heroku buildpacks.
- **Lint**: `ruff` (lint + format). Target Python 3.13. Line length 100.
- **Test**: `pytest` + `pytest-asyncio`. `tests/` mirrors `src/anthropic_throttle_proxy/` module layout.

## Load-bearing invariants

1. **The proxy hot path NEVER imports the `anthropic` SDK.** Hot path is aiohttp `Application` + raw `aiohttp.ClientSession` only. The SDK is *advisor-only* and lives behind `ADVISOR_ENABLED=true` so a transitive SDK bug can't kill the proxy.
2. **Bearer token never logged.** `bearer_id` is `sha256(token)[:12]` — only the hash appears in logs/metrics. Test with `tests/test_redaction.py`.
3. **AIMD floor (`THROTTLE_AIMD_MIN`) is the safety net.** When upstream hits sustained 429s, live cap shrinks to floor; floor must stay ≥ 1 so traffic never fully blocks. Hard-coded default 2.
4. **`/__throttle/health` must return in <50 ms.** Dokku healthcheck (`app.json`) polls it every 5 s with 5 s timeout. Anything that blocks the event loop here (sync I/O, large lock contention) breaks Dokku's restart policy.
5. **`THROTTLE_UPSTREAM` is the ONLY way to redirect traffic.** Never hard-code an upstream URL in source.
6. **The HTMX dashboard must render without JavaScript modules.** Pure HTMX 1.x (no Alpine, no React). One `<script>` tag for HTMX, server-rendered HTML. Catppuccin Mocha palette tokens only — no raw hex outside the tokens file.

## Don't break

- The `bearer_limiters` dict + `bearer_state` dict are read by Prometheus collectors; never mutate without holding `bearer_limiter_lock`.
- AIMD math (`shrink_on_pushback`, `ramp_on_success`) has a 60 s cooldown after each shrink — preserve the cooldown when refactoring.
- `prometheus_client` `CollectorRegistry` is process-local, not the default global one. Required because uvicorn-style workers would double-register metrics; we currently run single-worker but keep the registry isolated anyway.

## Local dev quickstart

```sh
uv sync
uv run python -m anthropic_throttle_proxy
# proxy on :8765, dashboard at /ui
uv run pytest                           # tests
uv run ruff check src tests             # lint
uv run ruff format src tests            # format
```

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
