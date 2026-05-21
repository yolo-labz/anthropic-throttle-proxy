# anthropic-throttle-proxy

Self-hosted reverse-proxy in front of `api.anthropic.com` that smooths request bursts and enforces a single per-bearer concurrency cap across an entire fleet of Claude Code / opencode / codex / Claude SDK clients.

Born out of [anthropics/claude-code#53915](https://github.com/anthropics/claude-code/issues/53915) — Anthropic's per-account concurrent-stream cap does not scale with the Max tier, so running 15+ parallel Claude TUIs through a single OAuth bearer produces a retry storm at the HTTP layer. This proxy puts the cap **once**, fairly, in a place you control.

## Features

- **Single-binary aiohttp proxy** — drop-in for `ANTHROPIC_BASE_URL`. Transparent forward to `https://api.anthropic.com`.
- **Two roles, same binary** — `local` (per-device passthrough, optional central fanout) and `central` (fleet-wide single semaphore).
- **Fair per-bearer concurrency** — round-robin across distinct client TCPs so no Claude TUI starves the others.
- **AIMD live ceiling** — shrinks the per-bearer cap on `429/503/529`, ramps back on consecutive 2xx successes. Self-regulating.
- **Burst pacing** — optional minimum gap between dispatches to upstream so 15 simultaneous requests get spaced over the millisecond budget instead of hitting Anthropic at the same instant.
- **HTMX live dashboard** — small dashboard at `/ui` showing in-flight/queued/served/AIMD state with live updates via SSE.
- **Cheap-AI advisor** — optional Anthropic Haiku integration that reads the running metrics and proposes knob tweaks (`MAX`, `QUEUE_MODE`, gap-ms) in natural language. Off by default.
- **Prometheus `/metrics`** — bring your own Grafana.
- **Dokku-deploy-ready** — Dockerfile + Procfile + app.json + healthcheck endpoints.

## Quick start (local)

```sh
git clone https://github.com/yolo-labz/anthropic-throttle-proxy.git
cd anthropic-throttle-proxy
uv sync
uv run python -m anthropic_throttle_proxy
# proxy listening on http://127.0.0.1:8765
# dashboard at http://127.0.0.1:8765/ui
```

Point your clients at it:

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
claude       # or opencode / codex / any SDK
```

## Deploy to Dokku

See [`docs/DEPLOY-DOKKU.md`](docs/DEPLOY-DOKKU.md). One-time:

```sh
ssh dokku@your.host
dokku apps:create anthropic-throttle
dokku ports:add anthropic-throttle http:80:8765
dokku config:set anthropic-throttle \
  CLAUDE_API_THROTTLE_MAX=8 \
  THROTTLE_QUEUE_MODE=fair \
  THROTTLE_MIN_DISPATCH_GAP_MS=50 \
  THROTTLE_HOST=0.0.0.0 \
  THROTTLE_PORT=8765
git remote add dokku dokku@your.host:anthropic-throttle
git push dokku main
```

Then point your devices at `https://anthropic-throttle.your.host`.

## Config reference

| Env | Default | Description |
|---|---|---|
| `CLAUDE_API_THROTTLE_MAX` | `32` | Per-bearer concurrent ceiling. AIMD adjusts the *live* value below this. |
| `THROTTLE_QUEUE_MODE` | `off` | `off` / `observe` / `fair` / `reactive`. Use `fair` on the central tier. |
| `THROTTLE_MIN_DISPATCH_GAP_MS` | `0` | Minimum gap between upstream dispatches in ms. Smooths bursts without capping throughput. **The 20/05/2026 ask.** |
| `THROTTLE_HOST` | `127.0.0.1` | Listen address. Use `0.0.0.0` inside containers. |
| `THROTTLE_PORT` | `8765` | TCP port. |
| `THROTTLE_UPSTREAM` | `https://api.anthropic.com` | Upstream target. |
| `THROTTLE_CENTRAL_URL` | *(unset)* | If set, the local proxy forwards each request to this URL first; on health-check failure it falls back direct-to-upstream. |
| `THROTTLE_AIMD_MIN` | `2` | Floor of the AIMD live ceiling. |
| `THROTTLE_AIMD_BACKOFF_S` | `60` | Cooldown after a 429 before ramping back up. |
| `THROTTLE_AIMD_RAMP_AFTER` | `10` | Consecutive 2xx responses required to bump the live ceiling by one. |
| `ADVISOR_ENABLED` | `false` | Enable the Haiku-driven advisor (`/ui/advisor`). Requires `ANTHROPIC_API_KEY`. |
| `ANTHROPIC_API_KEY` | *(unset)* | Used **only** by the advisor — never by the proxy path itself. |

## Architecture

```text
              ┌────────────────────────────────────────────────┐
              │            api.anthropic.com (upstream)        │
              └────────────────────▲──────────────────▲────────┘
                                   │ HTTPS            │ HTTPS
                  ┌────────────────┘                  └─────────┐
                  │                                              │
        ┌─────────┴─────────┐                          ┌────────┴───────┐
        │ central proxy     │   ──── tailnet ────►     │ local proxy    │
        │ (this binary)     │  ◄──── fallback ────     │ (this binary)  │
        │ MAX=8 fair        │                          │ off (passthru) │
        │ gap=50 ms         │                          │                │
        └────────▲──────────┘                          └────────▲───────┘
                 │                                              │
                 │                          ┌───────────────────┴────────────────────┐
                 │                          │  claude-code / opencode / codex / SDK  │
                 │                          │  (ANTHROPIC_BASE_URL=http://local:8765)│
                 │                          └────────────────────────────────────────┘
                 │
        ┌────────┴────────┐
        │ HTMX dashboard  │  /ui   ◄── you
        │ Haiku advisor   │  /ui/advisor (optional)
        │ Prometheus      │  /metrics
        └─────────────────┘
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design write-up.

## License

MIT — see [LICENSE](LICENSE).

## Author

Pedro H S Balbino (`@phsb5321`). Migrated out of the NixOS monorepo at [`phsb5321/NixOS`](https://github.com/phsb5321/NixOS) PRs #543/#549/#552/#553/#557/#562/#573/#575/#577/#580/#581 (20/05/2026 — this repo is the standalone successor with Dockerfile/Dokku and HTMX dashboard).
