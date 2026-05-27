---
name: deploy-dokku
description: Use when deploying, checking, or repairing the central Dokku-hosted anthropic-throttle-proxy instance, including app healthchecks, config, ports, logs, and central/local routing verification.
---

# Deploy Dokku

## Preflight

- Read `docs/DEPLOY-DOKKU.md`.
- Use `rbw` before asking Pedro for credentials or tokens.
- Distinguish local desktop health from central Dokku health. The local proxy may be healthy while central is down, and vice versa.

## Checks

```sh
curl -fsS https://anthropic-throttle.<host>/__throttle/health | jq
curl -fsS https://anthropic-throttle.<host>/metrics | head
ssh dokku@<host> apps:report anthropic-throttle
ssh dokku@<host> config:show anthropic-throttle
ssh dokku@<host> logs anthropic-throttle -n 120
```

## Deployment Rules

- Dokku healthchecks use `/__throttle/health`, not `/health`.
- Container must listen on `0.0.0.0:8765`.
- Central mode should use `THROTTLE_QUEUE_MODE=fair`.
- After deploy, verify both central health and local desktop `central_status`.
- Do not report success until Dokku checks pass and the desktop local proxy sees `central_status=up`.

## Deploy + verify sequence

```sh
# 1. Confirm green main + worktree clean
git status -s
git fetch origin main && git log --oneline origin/main..HEAD

# 2. Push to Dokku (parent reviews diff before pushing — never let a child agent push)
git push dokku main

# 3. Wait for Dokku checks to pass
ssh dokku@<host> ps:report anthropic-throttle | grep -E 'Status|Restart'

# 4. Verify central health from the wire (not Dokku internal)
curl -fsS https://anthropic-throttle.<host>/__throttle/health \
  | jq '{queue_mode, inflight, queued, served, central_status, version}'

# 5. Confirm desktop local proxy sees central
curl -fsS http://127.0.0.1:8765/__throttle/health \
  | jq '{central_status, central_url}'

# 6. Smoke a real upstream call through the chain (no token logged)
curl -fsS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $(rbw get anthropic-api-key)" \
  -X POST http://127.0.0.1:8765/v1/messages \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":4,"messages":[{"role":"user","content":"hi"}]}'
```

## Anti-patterns

- Reading Dokku container logs and declaring central healthy — container "started" ≠ proxy is serving. Always curl the public URL.
- Skipping the desktop `central_status` check — central can be up while the local poll loop is stuck (process leaks, network races). Both sides must agree.
- Re-tagging a release after a botched publish — cut `vX.Y.Z+1` instead (see `~/.claude/CLAUDE.md` release-engineering invariants).
- Pushing to Dokku from a feature worktree before merging to `main` — Dokku's `main` deploy branch is the source of truth; never deploy uncommitted state.

## Credentials

`rbw` first for `GROQ_API_KEY` (advisor), Dokku ssh keys, and any TLS material. Never paste secrets into Dokku `config:set` from shell history; pipe from `rbw get` directly.
