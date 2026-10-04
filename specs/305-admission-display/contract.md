# 305 — FROZEN CONTRACT: trusted admission display (owner: pK)

Base: `b5a08ee0` (fresh origin/main). Worktree/branch: `305-admission-display`.
Frozen 04/10/2026 03:1x BRT by coordinator per Mission Control
portfolio-throttler. Clarification 03:23 BRT folded in below.

## Allowed paths (ONLY these) — canonical full paths

- `src/anthropic_throttle_proxy/ui/routes.py`
- ONE new uniquely named admission-display test file
  (`tests/test_admission_display_trusted.py` — unique; never a shared name)
- `specs/305-admission-display/route-matrix.md` (source-only intended
  client/ingress route matrix)
- `specs/305-admission-display/**` (contract + result notes)

(The contract's short `ui/` paths are shorthand for the canonical
`src/anthropic_throttle_proxy/ui/...` paths, not extra paths.)

## Forbidden

`proxy.py`, `config.py`, `forwarding.py`, `prospective_runtime.py`, any pJ300
or pM303 WIP, any other file. No runtime/registry activation, no restarts/
deploys, no provider calls, no main writes, no hook bypass, no weakening tests.

## Technical contract (03:23 clarification folded in)

1. The source change is exactly: **existing local admission predicate ->
   existing status projection, plus visible-count scope**. Nothing else.
2. **Never** provider/HTTP scans from render callbacks (the UI render path must
   not perform network I/O of any kind).
3. **Never fabricate health**, never revive genuinely rejected Anthropic
   capacity, **never invent a new trusted map/health API**.
4. Account B: the pF account-map evidence is a **sanitized project receipt** —
   NOT a new runtime schema and NOT a per-account acceptance verdict to
   hardcode. The route matrix keeps **B client-path-unaccepted** verbatim; the
   UI must NOT hardcode B state and must NOT read raw private account evidence.
   Real binding/local admission already exist. Do NOT reuse old B-unknown
   snapshots. No private account evidence ever enters MiMo-facing or public
   output.

## Verify gate (per exact head)

- Red-capable targeted synthetic tests FIRST: the new admission-display test
  file (`uv run pytest tests/test_admission_display_trusted.py`).
- Then green + `uv run ruff check/format` on touched files + normal hooks.
- Normal hosted PR CI; no heavy desktop suite/build, no extra CI sweep.
- Deliverable: executed test results at exact head + published PR URL +
  route-matrix artifact.
