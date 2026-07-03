<p align="center">
  <img src="assets/brand/lockup.svg" alt="anthropic-throttle-proxy — fleet-wide pacing in front of api.anthropic.com" width="460">
</p>

# anthropic-throttle-proxy

Self-hosted reverse-proxy in front of `api.anthropic.com` that smooths request bursts and enforces a single per-bearer concurrency cap across an entire fleet of Claude Code / opencode / codex / Claude SDK clients.

Born out of [anthropics/claude-code#53915](https://github.com/anthropics/claude-code/issues/53915) — Anthropic's per-account concurrent-stream cap does not scale with the Max tier, so running 15+ parallel Claude TUIs through a single OAuth bearer produces a retry storm at the HTTP layer. This proxy puts the cap **once**, fairly, in a place you control.

## Features

- **Single-binary aiohttp proxy** — drop-in for `ANTHROPIC_BASE_URL`. Transparent forward to `https://api.anthropic.com`.
- **Two roles, same binary** — `local` (per-device passthrough, optional central fanout) and `central` (fleet-wide single semaphore).
- **Fair per-bearer concurrency** — round-robin across distinct client TCPs so no Claude TUI starves the others.
- **AIMD live ceiling** — shrinks the per-bearer cap on rate pushback (`429/503`, CUBIC-style ×0.7), ramps back on consecutive 2xx successes. `529` (upstream overloaded) is counted separately and does **not** shrink the cap — it's Anthropic's capacity, not your usage.
- **Header-aware pacing** — captures `anthropic-ratelimit-*` headroom + honors `Retry-After` (uncapped) per bearer, surfaced on `/__throttle/health`, `/metrics`, and the dashboard.
- **Z.ai Coding Plan quota gates** — parses z.ai JSON error bodies (`1308`/`1316`/`1317`) into local Retry-After windows because z.ai does not send a `Retry-After` header for plan exhaustion. Concurrency pushback (`1302`) still drives AIMD shrink.
- **OAuth utilization awareness** — Claude Code Max/Pro tokens are gated by 5h-rolling + 7d-weekly windows, reported as `anthropic-ratelimit-unified-*` *utilization* (not remaining counts). The proxy surfaces utilization, **auto-pauses a bearer until reset when a window is already `rejected`** (preempting the 429 + connection-reset storm), and — opt-in via `THROTTLE_UTILIZATION_TARGET` — proactively eases off as you approach the cap.
- **Burst pacing** — optional minimum gap between dispatches to upstream so 15 simultaneous requests get spaced over the millisecond budget instead of hitting Anthropic at the same instant.
- **HTMX live dashboard** — small dashboard at `/ui` showing in-flight/queued/served/AIMD state, refreshed by HTMX polling every 2s (server-rendered, no JS modules, no SSE).
- **Cheap-AI advisor (GROQ)** — optional, Anthropic-independent. On a throttle event (429/503/529) it fires a debounced, out-of-band GROQ call that reads the live metrics and proposes knob tweaks (`MAX`, `QUEUE_MODE`, gap-ms) in natural language — surfaced to the log + dashboard. Independent provider on purpose: asking Anthropic for advice during an Anthropic 429 storm hits the same limit. Off by default; also available on demand via `/ui/advisor`.
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
  CLAUDE_API_THROTTLE_MAX=3 \
  THROTTLE_QUEUE_MODE=fair \
  THROTTLE_MIN_DISPATCH_GAP_MS=50 \
  THROTTLE_MAX_HOLD_RETRY_AFTER_S=60 \
  THROTTLE_HOST=0.0.0.0 \
  THROTTLE_PORT=8765
git remote add dokku dokku@your.host:anthropic-throttle
git push dokku main
```

Then point your devices at `https://anthropic-throttle.your.host`.

## Config reference

| Env | Default | Description |
|---|---|---|
| `CLAUDE_API_THROTTLE_MAX` | `3` | Per-bearer concurrent ceiling. AIMD adjusts the *live* value below this. Current Opus-heavy Claude Code evidence says 4-5 can still 429. |
| `THROTTLE_QUEUE_MODE` | `off` | `off` / `observe` / `fair` / `reactive`. Use `fair` on the central tier. |
| `THROTTLE_MIN_DISPATCH_GAP_MS` | `0` | Minimum gap between upstream dispatches in ms. Smooths bursts without capping throughput. **The 20/05/2026 ask.** |
| `THROTTLE_HOST` | `127.0.0.1` | Listen address. Use `0.0.0.0` inside containers. |
| `THROTTLE_PORT` | `8765` | TCP port. |
| `THROTTLE_UPSTREAM` | `https://api.anthropic.com` | Upstream target. |
| `THROTTLE_CENTRAL_URL` | *(unset)* | If set, the local proxy forwards each request to this URL first; on health-check failure it falls back direct-to-upstream. |
| `THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT` | `2` | Local safety cap used only when `THROTTLE_CENTRAL_URL` is set and `THROTTLE_QUEUE_MODE=off`; prevents same-host Claude Code bursts from bypassing local admission before central/AIMD feedback arrives. |
| `THROTTLE_AIMD_MIN` | `1` | Floor of the AIMD live ceiling. Must stay ≥ 1 so traffic never fully blocks. |
| `THROTTLE_AIMD_INITIAL_CONCURRENT` | `1` | Live cap assigned to a new bearer before the proxy has evidence. AIMD grows from here after successful traffic and shrinks on pushback. |
| `THROTTLE_AIMD_BACKOFF_S` | `30` | Cooldown after a shrink before ramping back up. Applied as the pause only for a **budget** soft-throttle 429 (unified windows `allowed_warning`/`rejected` or past the warn line). |
| `THROTTLE_CONCURRENCY_COOLDOWN_S` | `2` | Pause for a **concurrency/rate** 429/503 — one with no `Retry-After` whose unified budget windows are still `allowed` below the warn line. AIMD shrink already sheds the load; a short cooldown just lets inflight drain instead of collapsing the account to cap=1 for 30 s. |
| `THROTTLE_AIMD_RAMP_AFTER` | `10` | Consecutive 2xx responses required to bump the live ceiling by one. |
| `THROTTLE_AIMD_DECREASE` | `0.7` | Multiplicative-decrease factor on rate pushback. `0.5` = TCP-Reno (deep cut), `0.7` = CUBIC (gentler, stays nearer the limit). |
| `THROTTLE_RATE_PUSHBACK_RETRIES` | `1` | Buffered retry count for upstream `429`/`503`/`529` before the proxy returns the pushback response to the client. Uses `Retry-After` when present; otherwise budget pushback uses `THROTTLE_AIMD_BACKOFF_S` and concurrency/rate pushback uses `THROTTLE_CONCURRENCY_COOLDOWN_S`. |
| `THROTTLE_MAX_HOLD_RETRY_AFTER_S` | `60` | Largest upstream `Retry-After` window held inside the local request before retrying. Keeps short temporary throttles hidden from Claude Code while still fast-failing multi-hour account windows. |
| `THROTTLE_ZAI_QUOTA_RESET_JITTER_S` | `15` | Extra seconds added to z.ai body reset times before reopening a quota-gated bearer. Avoids all clients sharing one key resuming at the exact reset second. |
| `THROTTLE_UTILIZATION_TARGET` | `0` | OAuth only. When `>0` (e.g. `0.9`), proactively shrinks the ceiling once the binding 5h/7d window utilization crosses this — eases off *before* hitting "rejected". `0` = surface utilization only. |
| `THROTTLE_PRIORITY_RESERVE_SLOTS` | `2` | Dedicated dispatch pool for short/latency-sensitive calls (Stop-hook evaluators: small `max_tokens`, no tools, small body), independent of the main AIMD pool so they never queue-starve behind long generations. Total upstream concurrency ≤ live cap + this. `0` disables the lane (priority calls demote to normal round-robin). |
| `THROTTLE_PRIORITY_MAX_TOKENS` | `8192` | A request classifies as priority only when its body parses with `0 < max_tokens ≤` this and no `tools`. |
| `THROTTLE_PRIORITY_MAX_BODY_BYTES` | `262144` | Priority also requires the request body ≤ this (256 KiB default) — `max_tokens` caps only the output, so a giant no-tools prompt must not jump the queue. |
| `ADVISOR_ENABLED` | `false` | Enable the GROQ advisor (auto-fires on throttle + `/ui/advisor`). Requires `GROQ_API_KEY`. |
| `GROQ_API_KEY` | *(unset)* | Used **only** by the advisor — never by the proxy path itself. |
| `ADVISOR_MODEL` | `llama-3.1-8b-instant` | GROQ model for the advisor diagnosis. |
| `ADVISOR_DEBOUNCE_S` | `120` | Minimum seconds between auto-advisor calls, so a 429 storm can't become a GROQ storm. |

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
        │ GROQ advisor    │  /ui/advisor + auto-on-throttle (optional)
        │ Prometheus      │  /metrics
        └─────────────────┘
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design write-up.

## Supply chain

Every release (a `vX.Y.Z` tag, or a manual `Release attestation` dispatch)
builds the Docker image and publishes **build-provenance + SBOM attestations**
bound to its registry digest through GitHub's native attestation API (keyless
OIDC — no long-lived signing keys). Both a CycloneDX and an SPDX SBOM are
attested. See [`.github/workflows/attest.yml`](.github/workflows/attest.yml).

Verify a published image before trusting it:

```sh
gh attestation verify \
  oci://ghcr.io/yolo-labz/anthropic-throttle-proxy:<tag> \
  --owner yolo-labz
```

A green result proves the image was built by this repo's workflow from the
tagged commit — not rebuilt or tampered with downstream.

CI gate: every PR is red unless `ruff check`, `ruff format --check`, and the
full `pytest` suite pass ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## License

MIT — see [LICENSE](LICENSE).

## Author

Pedro H S Balbino (`@phsb5321`). Migrated out of the NixOS monorepo at [`phsb5321/NixOS`](https://github.com/phsb5321/NixOS) PRs #543/#549/#552/#553/#557/#562/#573/#575/#577/#580/#581 (20/05/2026 — this repo is the standalone successor with Dockerfile/Dokku and HTMX dashboard).
