# 304 — FROZEN CONTRACT: OpenAI prompt/cache conservation (owner: pH)

Base: `b5a08ee0` (fresh origin/main, "feat(prospective): wire default-off
dispatch admission (#297)"). Worktree/branch: `304-openai-conservation`.
Frozen 04/10/2026 03:1x BRT by coordinator per Mission Control
portfolio-throttler.

## Allowed paths (ONLY these) — canonical full paths

- `src/anthropic_throttle_proxy/ratelimit.py`
- `tests/test_ratelimit_usage.py`
- `tests/test_tps_gauge.py`
- `src/anthropic_throttle_proxy/ui/templates/partials/stats.html` —
  **gauge-label portion ONLY**, if needed (the contract's short `ui/` paths are
  shorthand for these canonical paths, not extra paths)
- `specs/304-openai-conservation/**` (this contract + result notes)

## Forbidden

`proxy.py`, `config.py`, `forwarding.py`, `prospective_runtime.py`, any pJ300
or pM303 WIP, any other file. No runtime/registry activation, no restarts/
deploys, no provider calls, no main writes, no hook bypass, no weakening tests.

## Technical contract

1. Normalized OpenAI `input` and `cache_read` must be **disjoint** and
   **conserve `prompt_tokens`** (their accounting sum equals the parser's
   prompt_tokens meaning — no double counting, no silent drop).
2. Do NOT silently switch the history/gauge meaning between **total** and
   **fresh** merely to shrink a number. Existing history counts TOTAL accounted
   input including cache; prove the real chain
   `parser -> record_usage -> history -> gauge` and label the gauge truthfully
   for whatever it actually counts.
3. If a truthfully-fresh-input display would need `proxy.py` (reserved), keep
   that as a SEPARATE dependent change — report it, do not edit it. The
   independent conservation fix lands first.

## Verify gate (per exact head)

- Red-capable targeted synthetic tests FIRST:
  `uv run pytest tests/test_ratelimit_usage.py tests/test_tps_gauge.py`
- Then green + `uv run ruff check/format` on touched files + normal hooks.
- Normal hosted PR CI; no heavy desktop suite/build, no extra CI sweep.
- Deliverable: executed test results at exact head + published PR URL.
