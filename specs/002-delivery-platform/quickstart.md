# Quickstart: Prove the Throttle Proxy Works

Smallest "I just deployed this, prove it works" recipe. Walks through
local and central tiers; takes < 5 minutes end-to-end. Every step has a
concrete expected output.

## Prerequisites

- Python 3.13+ and `uv` installed.
- Anthropic-compatible client (`claude-code`, `opencode`, `codex`, or
  Anthropic SDK).
- Optional, for the advisor: `GROQ_API_KEY` in Bitwarden as
  `api/groq`; fetched via `rbw get api/groq`.

## 1. Local tier — dev mode

```sh
cd ~/Documents/Code/yolo-labz/anthropic-throttle-proxy
uv sync
uv run python -m anthropic_throttle_proxy
```

Expected stderr: a single line like
```
[anthropic-throttle] listening on 127.0.0.1:8765 max_concurrent=32 queue_mode=off upstream=https://api.anthropic.com central=(direct) dispatch_gap_ms=0
```

In a second terminal, hit the probe surface:

```sh
curl -fsS http://127.0.0.1:8765/__throttle/health | jq .
curl -fsS http://127.0.0.1:8765/
curl -fsSI http://127.0.0.1:8765/   # HEAD
```

Expected:
- `/__throttle/health` returns the JSON shape from
  [contracts/health-json.md](./contracts/health-json.md). `central_status`
  is `unknown` (no central configured), `served` is `0`.
- `GET /` returns `200 anthropic-throttle-proxy`.
- `HEAD /` returns `200` with no body.

Point a client:

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
claude-code   # or opencode / codex / your SDK
```

Send one request. Re-curl `/__throttle/health`. Expected change:
`served` increments by 1; a `bearers[<bid>]` entry appears with
`live_cap ≥ 1`.

## 2. Local tier — central-backed mode

```sh
export THROTTLE_CENTRAL_URL=http://anthropic-throttle.<your-host>  # https only if Dokku terminates TLS
uv run python -m anthropic_throttle_proxy
```

After 30 s (one `THROTTLE_CENTRAL_HEALTH_INTERVAL`), `/__throttle/health`
should report `central_status=up` and `central_last_check` should be a
recent epoch. Forced fallback test:

1. Block the central URL via `/etc/hosts` or a firewall rule.
2. Within 30 s, the local proxy reports `central_status=down`.
3. The next request from `claude-code` still completes — direct fallback
   is transparent.
4. Restore the central URL. Within 30 s, `central_status=up` returns.

## 3. Central tier — Dokku deploy

```sh
ssh dokku@<your-host>
dokku apps:create anthropic-throttle
dokku ports:add anthropic-throttle http:80:8765
dokku config:set anthropic-throttle \
  CLAUDE_API_THROTTLE_MAX=8 \
  THROTTLE_QUEUE_MODE=fair \
  THROTTLE_MIN_DISPATCH_GAP_MS=50
dokku checks:enable anthropic-throttle
```

Locally, push the central:

```sh
git remote add dokku dokku@<your-host>:anthropic-throttle
git push dokku main
```

Wait for the build. Then:

```sh
curl -fsS http://anthropic-throttle.<your-host>/__throttle/health | jq .  # use https only with TLS terminator
```

Expected: 200 with the health JSON. `queue_mode=fair`,
`max_concurrent=8`, `min_dispatch_gap_ms=50`. The Dokku healthcheck
should be green within 30 s.

## 4. Persistence verification (NixOS desktop only)

After any Nix / Home Manager change touching the user service:

```sh
# What runs after reboot (the persistent unit):
systemctl --user cat anthropic-throttle-proxy.service | grep ExecStart

# What runs RIGHT NOW (the effective unit):
systemctl --user show anthropic-throttle-proxy.service -p ExecStart --value
```

Both lines must print the SAME `/nix/store/<hash>-...` path. If they
diverge, the niri-guard activation gap deferred Home Manager activation
and a reboot will silently regress the service. Apply the surgical
symlink swap from `CLAUDE.md` § "Persistence checklist".

## 5. Advisor smoke test (optional)

```sh
export ADVISOR_ENABLED=true
export GROQ_API_KEY=$(rbw get api/groq)
uv run python -m anthropic_throttle_proxy
```

Open `http://127.0.0.1:8765/ui` in a browser. Trigger any throttle
event (parallel requests, large prompts, or simply call
`POST /ui/advisor` directly). Expected: within ~5 s the dashboard's
advisor panel shows a one-paragraph GROQ verdict. `state["last_advisor"]`
in `/__throttle/health` carries the same text.

If you see `503` from `POST /ui/advisor`, either `ADVISOR_ENABLED=false`
or `GROQ_API_KEY` is unset. The proxy refuses to import the advisor
module until both gates pass — that is constitution Principle I in
action.

## 6. Test and lint gates

```sh
uv run pytest                    # 89 tests, ~2.5s
uv run ruff check src tests      # zero findings expected
uv run ruff format --check src tests
```

All three must pass before pushing. CI runs the same gates plus
SonarQube line coverage (≥ ~85% aspirational via `PROJECT_ANALYSIS_TOKEN`
when `SONAR_HOST_URL` is configured; not a numeric CI gate today — see
spec FR-021).

## What to do if something is wrong

- Hot path 5xx but `/__throttle/health` OK → upstream issue. Check
  `journalctl --user -u anthropic-throttle-proxy.service`.
- `/__throttle/health` slow (> 50 ms) → event-loop blocker. Capture
  `py-spy dump` on the main PID.
- `central_status=down` permanently with central confirmed up → DNS or
  TLS issue between local and central. `curl
  https://<central>/__throttle/health` from the local host.
- Reboot regressed the service → run the persistence checklist
  in `CLAUDE.md`.

For deeper incident triage, invoke the `throttle-incident` skill in
`.claude/skills/`. For deployment-specific failures, use
`deploy-dokku`. For systemd / Nix activation, use `nix-user-service`.
