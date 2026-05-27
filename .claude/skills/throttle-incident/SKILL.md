---
name: throttle-incident
description: Use when investigating Anthropic throttle proxy incidents, including local or central health failures, queue buildup, 429/503/529 storms, root-probe regressions, broken central fallback, or claims that the throttler is not working.
---

# Throttle Incident

## Workflow

1. Capture evidence before changing anything:
   - `curl -fsS http://127.0.0.1:8765/__throttle/health | jq`
   - `curl -fsS "$THROTTLE_CENTRAL_URL/__throttle/health" | jq` when central is configured
   - `journalctl --user -u anthropic-throttle-proxy.service -n 120 --no-pager`
   - `systemctl --user show anthropic-throttle-proxy.service -p ExecStart -p DropInPaths -p ActiveState -p SubState`
2. State the hypothesis and the falsifier in one sentence each.
3. Map the symptom to the exact path:
   - queue/starvation: `FairBearerLimiter`, `queued_per_client`, `THROTTLE_QUEUE_MODE`
   - upstream pushback: `429`, `503`, `Retry-After`, AIMD shrink/ramp
   - overload: `529`, retry path, no AIMD shrink
   - central routing: `central_health_loop`, `pick_target`, `THROTTLE_CENTRAL_URL`
   - probes: `/`, `/__throttle/health`, `/metrics`
4. Add or update a focused test for the failure path.
5. Run the smallest relevant pytest target, then full `uv run pytest` and `uv run ruff check src tests`.
6. Verify the live runtime after deployment or activation. Do not claim fixed from tests alone.

## Known traps

- `/health` is not the local health endpoint; use `/__throttle/health`.
- Root probes must be answered locally and must not be forwarded upstream.
- A healthy current process does not prove persistence; runtime systemd drop-ins can hide stale persisted units.
- Never log bearer tokens. Use `bearer_id` hashes only.
- `central_status=down` in `/__throttle/health` does not prove central is down — the local health loop polls every `THROTTLE_CENTRAL_HEALTH_INTERVAL` (default 30s); curl central directly before concluding.
- Queue mode `off` does NOT mean no admission control once `THROTTLE_CENTRAL_URL` is set: PR #28 caps same-host bursts at `THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT` (default 2). A "queue full" symptom with mode=off is by design.

## Falsifier examples (write one before acting)

Hypothesis → falsifier pairs that have caught real bugs:

- "Local proxy is stale" → falsifier: `pid=$(systemctl --user show ... -p MainPID --value); tr '\0' '\n' </proc/$pid/cmdline | grep <expected-store-path>`. If the cmdline matches the expected pkg, the hypothesis is wrong.
- "Central is dropping requests" → falsifier: `curl -fsS $THROTTLE_CENTRAL_URL/__throttle/health | jq .inflight,.queued`. If both are 0 over 60s while local reports `central_status=down`, the bug is in the local poll loop, not central.
- "AIMD floor is too low" → falsifier: scrape `anthropic_throttle_live_cap{bearer_id=…}` for 5 minutes. If cap never grows past the floor despite sustained 200s, fix is in `ramp_on_success`, not the floor.

## Adversarial review hand-off

Before merging any throttle-path fix: spawn the `codex:codex-rescue` agent (or hand to Codex via `~/codex` CLI) with the symptom, hypothesis, live evidence, diff, and verification plan. Block merge on its findings — see top-level `CLAUDE.md` §"Incident workflow and adversarial review".
