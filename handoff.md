# Handoff: throttle incident 25/05/2026

This handoff is for the next Claude Code session touching this repository.
Read it before changing throttle behavior, Nix pins, deployment, or host
activation.

## What was wrong

Pedro reported Claude Code requests sitting at "typed but no spinner" and
server-side rate limiting while the local proxy was configured as
`THROTTLE_QUEUE_MODE=off`.

The live system had two separate issues:

1. Central throttle was doing real queueing.
   `http://anthropic-throttle.home301server.com.br/__throttle/health` showed
   `queue_mode=fair`, active queued work, and high 7-day utilization. Some delay
   was expected because the central tier intentionally holds requests before
   upstream response start.

2. Local fallback was unsafe when central flapped.
   The desktop local tier was configured with `THROTTLE_QUEUE_MODE=off` because
   central owns normal fleet-wide queueing. When central went down or a central
   forward failed, the local tier retried direct upstream while still in
   pass-through mode. That meant a central flap could become an unqueued direct
   firehose against Anthropic.

The fix that shipped:

- `yolo-labz/anthropic-throttle-proxy#23`
  - merged as `78c2c43d48228ddd09e543a2219019209ffbd17e`
  - promotes local admission to `fair` when central is configured but routing is
    direct fallback
  - serializes emergency direct retries after a central forward failure
  - keeps promoted fair limiters from being downgraded back to `off`
- `phsb5321/NixOS#703`
  - merged as `6810250dcef68637d047ac6544911b4401deac30`
  - bumps the Nix package pin to the fixed upstream commit
- Host activation completed with:
  - `/nix/store/055538y8i9r96lycpq5mx2pc6yk6mhnv-nixos-system-desktop-26.05.20260523.3d8f0f3`
  - active user unit points at `/nix/store/ffl8ngpi6y4f49vps94r6w7w89nfsa59-anthropic-throttle-proxy-0.1.0`

## Mistakes made during the session

Do not repeat these.

- The first response treated sandbox restrictions as a stopping point too early.
  The right move was to keep looking for a route: GitHub API/`gh`, `/tmp`
  worktree/clone, Nix build with a writable cache, and host activation from a
  clean checkout.
- A temporary workspace proxy was started while systemd was also trying to own
  `127.0.0.1:8765`. That created the bind collision that made the fixed systemd
  unit restart-loop. If you start an emergency process, record its PID and kill
  it before handing control back to systemd.
- The first NixOS PR upload accidentally emptied
  `pkgs/anthropic-throttle-proxy/default.nix` by using Perl in-place editing
  with shell redirection. Always parse/check generated Nix files before upload.
- Local dirty main was left behind from the emergency code edit. Future doc or
  follow-up work must use a dedicated worktree first.
- Early checks looked at the symptom, but the important proof came from
  combining health JSON, journal lines, and the exact fallback path in code.
  Similar-looking rate-limit incidents are not enough evidence.

## How Claude should have solved it

The correct incident workflow is:

1. Capture live state first:
   - `curl http://127.0.0.1:8765/__throttle/health | jq`
   - central health at `THROTTLE_CENTRAL_URL`
   - `journalctl --user -u anthropic-throttle-proxy -n 200 --no-pager`
   - current `ANTHROPIC_BASE_URL`, service `ExecStart`, and Nix store path
2. State the hypothesis in one sentence.
   Example: "Local central fallback is bypassing the local queue in `off` mode,
   so central flaps create direct unqueued Anthropic bursts."
3. Prove or falsify that hypothesis against the exact code path:
   - `pick_target()`
   - `_get_bearer_limiter()`
   - `_retry_direct_once()`
   - queue mode transitions in `FairBearerLimiter`
4. Apply the smallest live mitigation that is reversible:
   - `/ui/config` or direct POST to set `min_dispatch_gap_ms`
   - verify it persisted in
     `~/.local/state/anthropic-throttle-proxy/overrides.json`
5. Patch the repo in a feature worktree and test:
   - targeted forwarding tests
   - full pytest
   - ruff
6. Publish and merge the upstream proxy fix.
7. Bump the NixOS package pin and fixed-output hash.
8. Wait for NixOS CI, including host eval.
9. Activate the host from a clean checkout, not the dirty repo checkout.
10. Verify the actual runtime:
    - user unit `ExecStart` points to the fixed store path
    - `curl http://127.0.0.1:8765/__throttle/health` responds
    - journal shows the fixed store path and no bind failure

## Required adversarial review

Before merging a throttle behavior change, ask Codex for adversarial review.
The review prompt must include:

- the user-visible symptom
- the one-sentence hypothesis
- live health/journal evidence
- the exact code diff
- test results
- the deployment/pin/activation plan

Codex must be asked to look specifically for:

- unproven causality
- fallback paths that still bypass local queueing
- limiter mode transitions that can strand queued futures
- central/local behavior mismatches
- Nix pin or fixed-output hash mistakes
- host activation gaps where merged code is not actually running

Do not merge or declare the incident solved until the adversarial findings are
addressed or explicitly documented with evidence.
