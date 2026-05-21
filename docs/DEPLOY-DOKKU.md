# Deploy on Dokku

End-to-end deploy of `anthropic-throttle-proxy` to a Dokku host via the Dockerfile builder.

Tested against Dokku ≥ 0.34 (Dockerfile + `app.json` healthchecks). Pedro's home server runs Dokku at `dokku.home301server.com.br` over Tailscale.

## Prerequisites

- A Dokku host you can SSH to as `dokku@<host>`.
- A DNS record (`A` / `AAAA`) pointing the public domain at the Dokku host (or a tailnet-only setup — see "Tailnet-only" below).
- An OAuth bearer token for `api.anthropic.com` is **not** required at deploy time. Clients supply their own via the standard `Authorization: Bearer …` header — the proxy forwards untouched.
- A Cloudflare token / similar is **not** required.

## One-time setup

```sh
# from your laptop
ssh dokku@your.host
```

```sh
# inside the Dokku host shell
dokku apps:create anthropic-throttle
dokku ports:add anthropic-throttle http:80:8765
dokku config:set anthropic-throttle \
  CLAUDE_API_THROTTLE_MAX=8 \
  THROTTLE_QUEUE_MODE=fair \
  THROTTLE_MIN_DISPATCH_GAP_MS=50 \
  THROTTLE_HOST=0.0.0.0 \
  THROTTLE_PORT=8765
dokku checks:enable anthropic-throttle
# Optional: enable the GROQ advisor (Anthropic-independent diagnosis on throttle)
# dokku config:set anthropic-throttle ADVISOR_ENABLED=true GROQ_API_KEY=gsk_…
```

## Push + deploy

```sh
# from your laptop, inside the repo clone
git remote add dokku dokku@your.host:anthropic-throttle
git push dokku main
```

Dokku will:
1. Detect the `Dockerfile` (no Procfile-only buildpack path; container-builder takes priority).
2. Build the image (multi-stage uv build — ~30 s warm).
3. Run the startup healthcheck (`curl -fsS http://localhost:8765/__throttle/health`) up to 30 times with 5 s timeouts; if it doesn't pass, the deploy aborts.
4. Switch traffic on the configured ports.
5. Keep liveness-checking the same endpoint after the app is up.

## Tailnet-only (no public exposure)

For Pedro's setup, the proxy is reachable only over Tailscale. Two patterns:

### A. Bind Dokku's nginx to the tailnet IP

Edit the Dokku nginx vhost template (e.g. via `dokku-tailscale` plugin) so the `listen` directive uses the tailnet IP instead of `0.0.0.0`. Browsers / clients hit the proxy through `http://anthropic-throttle.<tailnet-host>`. Public DNS resolves to a Tailscale IP that only logged-in tailnet peers can reach.

### B. Skip Dokku's port-mapping entirely

If `dokku-tailscale` isn't available, drop `dokku ports:add` and instead bind the container directly to a tailnet-only IP via `dokku docker-options`. Slightly more manual but no nginx layer.

## Pointing your clients

```sh
# global env in your shell rc
export ANTHROPIC_BASE_URL=https://anthropic-throttle.<your-host>
```

That single line turns the proxy on for `claude-code`, `opencode`, `codex`, the Anthropic Python/Node SDKs, and any other client that honours `ANTHROPIC_BASE_URL`. No token rewriting; the client's `Authorization: Bearer …` flows through unchanged.

## Operations

| Action | Command |
|---|---|
| Tail logs | `dokku logs anthropic-throttle --tail` |
| Restart | `dokku ps:restart anthropic-throttle` |
| Bump knob | `dokku config:set anthropic-throttle CLAUDE_API_THROTTLE_MAX=5` (auto-restarts) |
| Stop (clients fall back to direct upstream via the local proxy's circuit-breaker) | `dokku ps:scale anthropic-throttle web=0` |
| Resume | `dokku ps:scale anthropic-throttle web=1` |
| Inspect health | `curl https://anthropic-throttle.<host>/__throttle/health \| jq` |
| Prometheus | `curl https://anthropic-throttle.<host>/metrics` — scrape into Grafana |
| Open dashboard | `https://anthropic-throttle.<host>/ui` |

## Troubleshooting

- **`Multiple versions of pnpm specified`** — wrong app, this is the Python project. You're running the wrong git remote.
- **`curl: (7) Failed to connect to localhost port 8765`** during startup healthcheck — the container isn't listening on 8765 inside the container. Confirm `THROTTLE_HOST=0.0.0.0` and `THROTTLE_PORT=8765` are set via `dokku config`.
- **High `retries=` in `/__throttle/health`** — your per-bearer concurrent ceiling is too high for the Anthropic tier you're on. Lower `CLAUDE_API_THROTTLE_MAX`. Max tier reality is ~5.
- **`disconnects=` climbing** — clients are giving up before upstream answers. Increase the proxy's keep-alive window or check that your reverse-proxy timeout isn't shorter than long-running streaming responses.

## Rollback

Dokku keeps the previous image. Roll back with:

```sh
dokku ps:rebuild anthropic-throttle  # rebuild from current git
# or
dokku releases:rollback anthropic-throttle 1   # one release back
```

If a config tweak misbehaves, revert via `dokku config:unset anthropic-throttle THROTTLE_MIN_DISPATCH_GAP_MS` — auto-restarts back into the previous config.
