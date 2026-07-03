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
- **Test**: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). `tests/` covers the proxy app, forwarding paths, pacing, unified-window parsing, and the advisor (~85% line coverage, gated in CI via SonarQube). New tests mirror `src/anthropic_throttle_proxy/` module layout.

## Architecture

Single-process aiohttp app, wired in `proxy.py::main()`:

- **Hot path** (`proxy.py::handler`) — catch-all `*` route forwards every request to `THROTTLE_UPSTREAM` (or `THROTTLE_CENTRAL_URL` if set), streams response back, parses SSE `usage` blocks for token/cost metrics.
- **Per-bearer fair queue** (`FairBearerLimiter`) — replaces a flat `asyncio.Semaphore`. Lazy-allocated per `bearer_id` under `bearer_limiter_lock`. Round-robin across `client_id` (peer host:port or `X-Throttle-Client-Id` header) so one chatty TUI can't starve a sibling.
- **Priority lane** (03/07/2026) — short/latency-sensitive calls (parsed `0 < max_tokens ≤ THROTTLE_PRIORITY_MAX_TOKENS`, no `tools`, body ≤ `THROTTLE_PRIORITY_MAX_BODY_BYTES`) dispatch from a DEDICATED pool of `THROTTLE_PRIORITY_RESERVE_SLOTS` (default 2), independent of the main AIMD pool, with its own per-client round-robin. Fixes /goal Stop-hook evaluator queue-starvation (24s eval waited 46s behind long Opus gens → 30s client timeout). Total upstream concurrency ≤ live cap + reserve; reserve 0 disables the lane (priority demotes to normal RR at `acquire()`, which returns the effective lane so release accounting stays symmetric). Lane calls still honor `Retry-After` + unified-window pauses. All three knobs hot-tunable via `/ui/config`.
- **AIMD reactive throttle** — per-bearer ceiling shrinks on rate pushback (`429/503`) by `THROTTLE_AIMD_DECREASE` (default 0.7, CUBIC-style), grows additively after `THROTTLE_AIMD_RAMP_AFTER` (default 10) successes past the `THROTTLE_AIMD_BACKOFF_S` (default 30 s) cooldown. Floor is `THROTTLE_AIMD_MIN` (default 1). `529` (upstream overloaded) is counted separately (`anthropic_overload_total`) and does NOT shrink — it's Anthropic capacity, not your usage.
- **Header-aware pacing** — `_extract_ratelimit` captures `anthropic-ratelimit-*` + `Retry-After` from each upstream response into `bearer_state[bid]["last_ratelimit"]`; `FairBearerLimiter.note_retry_after`/`wait_retry_after` honor `Retry-After` (uncapped) before the next dispatch and block `grow()` until the window closes.
- **OAuth unified-window pacing** (`_parse_unified`/`_apply_unified`) — Claude Code Max/Pro tokens return `anthropic-ratelimit-unified-*` (5h/7d *utilization* 0..1 + status + epoch reset), NOT the API-key remaining-count family (measured 21/05/2026). The proxy surfaces utilization (`bearer_state[bid]["unified"]`, gauges `anthropic_ratelimit_unified_{5h,7d}_utilization`), auto-pauses a bearer until reset when a window is `rejected`, emits a debounced early-warning (one log line + `anthropic_ratelimit_unified_warnings_total{bearer,window}`) once the binding window crosses `THROTTLE_UTILIZATION_WARN` (default 0.9, **warn-only — never shrinks**, the pre-`rejected` signal the journal previously lacked), and — when `THROTTLE_UTILIZATION_TARGET>0` (default 0/off) — proactively shrinks once the binding window crosses the target. Split into flat single-purpose helpers (`_publish_unified_gauges`/`_maybe_pause_rejected`/`_maybe_warn_unified`/`_maybe_glide`) to keep cognitive complexity low.
- **Burst pacing** — single process-global `_dispatch_lock` enforces `THROTTLE_MIN_DISPATCH_GAP_MS` between consecutive upstream POSTs. Orthogonal to `CLAUDE_API_THROTTLE_MAX` (which caps concurrency, not rate).
- **Queue modes** (`THROTTLE_QUEUE_MODE`): `off` (passthrough, no AIMD counters), `observe` (no queue but AIMD counters DO move — early-warning without slowdown), `fair`/`reactive` (queue + AIMD; `reactive` is an alias).
- **Central tier** — when `THROTTLE_CENTRAL_URL` is set, local proxy forwards there; background `central_health_loop` polls `/__throttle/health` every `THROTTLE_CENTRAL_HEALTH_INTERVAL`. Central unhealthy → transparent fallback to direct upstream. A local proxy with `THROTTLE_QUEUE_MODE=off` still uses a small fair queue capped by `THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT` (default 2) so same-host Claude Code bursts cannot bypass local admission before central/AIMD feedback arrives.
- **UI** (`ui/routes.py::attach_ui`) — HTMX 1.x dashboard at `/ui`, jinja2 templates in `ui/templates/`. Advisor in `ui/advisor_impl.py` (`recommend()`): a cheap GROQ diagnosis of throttle events. Fires automatically (debounced) from `proxy._maybe_advise` on 429/503/529 and on demand via `POST /ui/advisor`; latest result lives in `state["last_advisor"]`. Gated by `ADVISOR_ENABLED` + `GROQ_API_KEY`. Three optional dashboard panels (all UI-only, failure-tolerant, hidden when their env var is unset): **Accounts** (`THROTTLE_ACCOUNT_CRED_PATHS`, per-account 5h/7d usage via `/api/oauth/usage`), **Fleet** (`THROTTLE_FLEET_HEALTH=LABEL:url,...` cross-fetches sibling proxies' `/__throttle/health` so the z.ai `:8766` instance shows in one pane), **Copilot** (`THROTTLE_COPILOT_ORGS` + `THROTTLE_COPILOT_TOKEN` reads `/orgs/{org}/copilot/billing` — subscription/seats only; the individual-user usage API does not exist).
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

## Incident workflow and adversarial review

Throttle incidents are not solved by a plausible code patch. They are solved
only when the runtime symptom, code path, Nix pin, and host activation are all
verified.

Before changing throttle behavior, central fallback, limiter state, request
pacing, Nix pins, or deployment config:

1. Capture live evidence first: local `/__throttle/health`, central
   `/__throttle/health`, `journalctl --user -u anthropic-throttle-proxy`, the
   current user unit `ExecStart`, and the current Nix store path.
2. State the hypothesis in one sentence.
3. Identify what would falsify the hypothesis before editing.
4. Add or update tests that exercise the exact failure path.
5. Run targeted tests, full pytest, and ruff.
6. Verify the deployed runtime, not just the merged PR.

**Mandatory Codex adversarial review:** before merging any throttle-path fix or
declaring a throttle incident solved, ask Codex for adversarial review. Provide
Codex the symptom, hypothesis, live evidence, diff, tests, and deployment plan.
Codex must challenge causality, central/local fallback behavior, limiter mode
transitions, Nix fixed-output hashes, and whether the activated host is actually
running the new store path. Address the findings before merge; if a finding is
not acted on, document the evidence-backed reason.

See `handoff.md` for the 25/05/2026 incident and the mistakes that led to this
rule.

## Host service verification

Pedro's desktop runs this proxy as a Nix/Home Manager user service. Treat
`systemctl --user cat` and `systemctl --user show` as two different evidence
surfaces:

- `systemctl --user cat anthropic-throttle-proxy.service` shows the persisted
  unit plus persisted drop-ins. **This is what reboot will execute.**
- `systemctl --user show anthropic-throttle-proxy.service -p ExecStart -p
  FragmentPath -p DropInPaths -p ActiveState -p SubState` shows the effective
  runtime state after daemon reloads, transient overrides, and restarts.
  **This is what is currently running.**
- Runtime drop-ins under `/run/user/$(id -u)/systemd/user/...` survive
  `daemon-reload` but are wiped on logout/reboot. They can silently mask a
  stale persistent unit. **A transient `ExecStart=` override hides a stale
  base unit until the next reboot, at which point the service silently
  regresses.**

Full capture sequence (run in this order):

```sh
# 1. EFFECTIVE runtime (merged base+drop-ins, what systemd executes now)
systemctl --user show anthropic-throttle-proxy.service \
  -p ExecStart -p FragmentPath -p DropInPaths -p EnvironmentFiles \
  -p MainPID -p ActiveState -p SubState

# 2. BASE unit + drop-in chain (what reboot will resolve to)
systemctl --user cat anthropic-throttle-proxy.service

# 3. Persistent symlink target (Home Manager's published unit)
readlink ~/.config/systemd/user/anthropic-throttle-proxy.service

# 4. Drop-in inventory (persistent vs transient)
ls -la ~/.config/systemd/user/anthropic-throttle-proxy.service.d/
ls -la /run/user/$(id -u)/systemd/user/anthropic-throttle-proxy.service.d/

# 5. Wire-level health (proves the running PID actually serves traffic)
curl -fsS http://127.0.0.1:8765/__throttle/health | jq

# 6. Service journal (last failure window)
journalctl --user -u anthropic-throttle-proxy.service -n 80 --no-pager

# 7. Trace the Nix store path actually on the cmdline
pid=$(systemctl --user show anthropic-throttle-proxy.service -p MainPID --value)
tr '\0' '\n' </proc/$pid/cmdline | grep anthropic-throttle-proxy

# 8. HM profile pointer vs activated NixOS toplevel's HM closure
readlink -f ~/.local/state/nix/profiles/home-manager
nix-store -qR "$(readlink /run/current-system)" | grep home-manager-generation
# These two MUST match. If not, a `nh os switch` was deferred (niri-guard
# rewrites to `nh os boot`) and the persistent symlink is stale.
```

### Persistence checklist (apply after any HM / Nix / systemd-user fix)

Run this checklist verbatim. Do not declare persistence fixed without all
boxes ticked.

1. **Surgical symlink swap** if HM activation was deferred (niri-guard host):
   ```sh
   TOPLEVEL=$(readlink /run/current-system)
   # Resolve HM-files deterministically. The old recipe ran
   #   nix-store -qR "$TOPLEVEL" | grep home-manager-files | head -1
   # over the FULL transitive closure, so on a multi-user / multi-generation
   # store `head -1` could pick a STALE sibling's files dir (the 26/05/2026
   # stale-unit incident). Instead: find the single home-manager-generation in
   # the activated closure, then take the single home-manager-files it directly
   # references. Fail loud on any ambiguity rather than silently mis-pick.
   HM_GEN=$(nix-store -qR "$TOPLEVEL" | grep -E 'home-manager-generation$')
   [ "$(printf '%s\n' "$HM_GEN"   | grep -c .)" -eq 1 ] || { echo "ambiguous home-manager-generation:"; printf '%s\n' "$HM_GEN"; return 1 2>/dev/null || exit 1; }
   HM_FILES=$(nix-store -q --references "$HM_GEN" | grep -E 'home-manager-files$')
   [ "$(printf '%s\n' "$HM_FILES" | grep -c .)" -eq 1 ] || { echo "ambiguous home-manager-files:"; printf '%s\n' "$HM_FILES"; return 1 2>/dev/null || exit 1; }
   # verify ExecStart in that HM-files's unit BEFORE swapping
   grep ExecStart "$HM_FILES/.config/systemd/user/anthropic-throttle-proxy.service"
   ln -sfn "$HM_FILES/.config/systemd/user/anthropic-throttle-proxy.service" \
     ~/.config/systemd/user/anthropic-throttle-proxy.service
   ```
2. **`systemctl --user daemon-reload`** so systemd re-reads the chain.
3. **Verify base and effective ExecStart match** (no transient drop-in masking):
   ```sh
   systemctl --user cat anthropic-throttle-proxy.service | grep ExecStart
   systemctl --user show anthropic-throttle-proxy.service -p ExecStart --value
   ```
4. **Remove redundant transient drop-ins**:
   ```sh
   /run/current-system/sw/bin/rm -f \
     /run/user/$(id -u)/systemd/user/anthropic-throttle-proxy.service.d/override.conf
   systemctl --user daemon-reload
   ```
5. **Restart when safe, then re-verify**:
   ```sh
   pre=$(curl -fsS http://127.0.0.1:8765/__throttle/health | jq .served)
   systemctl --user restart anthropic-throttle-proxy.service
   sleep 2
   systemctl --user is-active anthropic-throttle-proxy.service
   curl -fsS http://127.0.0.1:8765/__throttle/health \
     | jq '{served,inflight,queue_mode,central_status}'
   ```
6. **Confirm pkg has the load-bearing code**. For PR #29 root probes:
   ```sh
   pkg=$(systemctl --user show anthropic-throttle-proxy.service \
     -p ExecStart --value | grep -oE '/nix/store/[a-z0-9]+-anthropic-throttle-proxy-0\.1\.0')
   grep -c 'def root_probe\|app.router.add_get("/", root_probe' \
     "$pkg/lib/python3.13/site-packages/anthropic_throttle_proxy/proxy.py"
   # expect >= 2
   ```
7. **Nix gcroot guard** — canonical HM-files must be reachable from a gcroot:
   ```sh
   nix-store --query --roots "$HM_FILES" | grep -E 'system-[0-9]+-link|/run/current-system'
   ```

### Stale-unit / root-probe incident (26/05/2026)

PR #29 (`fix: handle root probes locally`) shipped the new package and the
NixOS pin was bumped to `b1555ad`. But the persistent user-unit symlink kept
pointing at the pre-root-probe package because the host runs `niri-guard`,
which rewrites `nh os switch` → `nh os boot`. The runtime drop-in
(`/run/user/1000/systemd/user/anthropic-throttle-proxy.service.d/override.conf`)
masked the regression: `systemctl --user show` reported the new package, but
`systemctl --user cat` showed the old one. A reboot would have regressed the
service back to a build without root-probe handling, breaking `GET /` probes
for downstream tools.

Forensic chain:

- `~/.config/systemd/user/anthropic-throttle-proxy.service` → old
  `8b4zq94h...home-manager-files` → unit ExecStart `v3hcv7w...` (pre-PR-29).
- `/run/current-system` HM-files dep → `2yqrq9...home-manager-files` → unit
  ExecStart `mg70cbx3...` (PR-29 correct).
- HM profile pointer (`~/.local/state/nix/profiles/home-manager`) stuck at
  gen 488 = `wh3678v4...home-manager-generation` → pkg `xybx7arq...`
  (older than `v3hcv7w`).
- Three different generations live in store, only the runtime drop-in pinned
  the right one in memory.

Three durable lessons:

1. **Always use both `show` and `cat`.** `show` answers "what is running
   now"; `cat` answers "what will run after reboot". Disagreement means a
   transient drop-in is hiding stale state.
2. **Root probes must be handled locally.** The proxy forwards everything
   else to `THROTTLE_UPSTREAM`, but `GET /` and `HEAD /` are infrastructure
   probes (load balancers, Dokku healthchecks, curl smoke tests) that should
   not consume a bearer slot. PR #29 added a local 200 OK; any future code
   path that re-introduces the catch-all for `/` is a regression.
3. **Persistence != activation.** A merged PR + bumped Nix pin + green CI
   only proves the *artifact* is correct. Whether the *host* is running it
   requires the verification chain above.

## Repo worktree policy

This repo follows the global mandatory worktree-first rule
(`~/Documents/Code/CLAUDE.md` and `~/.claude/CLAUDE.md`).

- **Never edit on `main`.** `git rev-parse --show-toplevel` must end in a
  `-NNN-<slug>` directory before any `Edit` / `Write` / `Bash` mutation.
- **Use `.worktrees/<branch>` for in-repo feature work** (preferred when
  scope is bounded and short-lived; survives merge cleanly). Example:
  `.worktrees/anthropic-throttle-proxy-029-local-root-probe`.
- For cross-repo work coordinated with `~/NixOS`, use a sibling worktree
  under `~/NixOS-NNN-<slug>` for the NixOS half (the convention sibling
  agents already follow — see `git -C ~/NixOS worktree list`).
- **Never `git stash`** in this repo. Open another worktree.
- **Never push to `main` directly.** PR workflow only (conventional commit
  subject ≤ 72 chars, `Co-Authored-By` trailer, rebase before push, wait
  for CI, then `gh pr merge --squash --delete-branch`).
- Do not overwrite sibling-agent work in adjacent worktrees (e.g.
  `~/NixOS-719-home-zellij-tabs` for fonts/statusline). Coordinate by
  checking `git -C <sibling> log --oneline -5` before any edit that could
  conflict.

## Project-local skills

Agents should use the repo-local skills in `.claude/skills/` when the
request matches their trigger:

- **`throttle-incident`** — triage live throttle failures, central
  fallback, queue buildup, 429/503/529 storms, and root-probe regressions.
- **`nix-user-service`** — verify desktop/Home Manager activation for this
  user service and catch persistent-vs-runtime systemd mismatches.
- **`deploy-dokku`** — deploy or verify the central Dokku instance without
  confusing container health with local desktop health.

### Skill design notes

When authoring or editing the skills above:

- One skill per recurring incident shape. Do not bundle unrelated flows.
- Skills must capture **evidence first, hypothesis second**. The body of
  each `SKILL.md` is a checklist of commands + expected outputs, not a
  prose essay.
- Skills must reference the `Incident workflow and adversarial review`
  section above. Codex adversarial review is mandatory before declaring
  any throttle-path fix done.
- Skills must prefer commands already run and observed-good on the host. When a
  command is a template for another host, mark placeholders clearly.
- Keep `.claude/settings.json` minimal and task-specific. Do not blindly
  enable global skills (browser automation, generic frontend-design,
  unrelated spec/PR skills) that this repo does not need.

## Local dev quickstart

```sh
uv sync
uv run python -m anthropic_throttle_proxy   # proxy :8765, dashboard /ui, metrics /metrics, health /__throttle/health
uv run pytest                                # full suite (proxy/forwarding/pacing/unified/advisor)
uv run pytest tests/test_pacing.py::test_yyy # single test
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
