# Acceptance and delivery receipts

08/10/2026. Base `515f67d`; generator family OpenAI. Source-only migration.

- Baseline regression: **8 failed, 3 passed**, exposing absent canonical
  exports and product branding while legacy entrypoint dispatch passed.
- Fixed focused acceptance: **333 passed** (entrypoints, routing/chat budget,
  ingress/ADR-6a, registry, state overrides, security, quarantine, UI/enrollment).
- Full suite: **2,200 passed**, 290 existing warnings, **100.27 s**. Ruff lint
  and format pass; 135 Python files formatted. No test skip/suppression added.
- Wheel and sdist both include canonical/compatibility packages and UI resources.
  Each isolated install passes **11 entrypoint/UI checks** outside the checkout.
  A wheel rebuilt from the sdist has the same payload and SHA-256 as the wheel:
  `6659ce4509a454d92b0eb655f89ccb70cfa5e129918dc446c55c44370023e4e8`.
  Dispatch checks intercept `main`, avoiding listeners/collectors/provider calls;
  existing real HTTP fixture tests supply routing/security acceptance.
- Actual admitted limits: `cpu.max=100000 100000`, `memory.max=536870912`,
  `memory.swap.max=0`, `pids.max=64`. Full-suite peak **127.3 MiB**;
  final package acceptance peak **128.9 MiB**. Every compute payload used
  `desktop-job-admit --small`, single-thread settings, sequential execution.
- First launcher invocation rejected non-allowlisted `--env` keys; supplied the
  same thread-limiting variables as payload argv within unchanged admitted limits.
  Offline generic pip resolution lacked registry metadata; isolated dependencies
  instead came from the extracted sdist's **frozen uv lock**, followed by the
  built wheel's no-dependency install. No allocation or hook bypass.
- Dependency versions/hashes are unchanged. Gateway, ingress, routing/config,
  registry, account and metric implementations match the base byte-for-byte.
  No workflows/CODEOWNERS/live settings/provider requests were changed.

The current handoff exposes the canonical commands and retained contracts.
Final source SHA, exact PR-head CI/check URLs, unresolved-thread count, merge
state/SHA, exported artifacts and one-line source revert command are authoritative
in the unique secret-free machine receipt:

`/home/notroot/.local/state/throttler-recovery/2026-10-08-2704/2719-generic-agent-status.json`

Raw local logs and package receipts share that directory, prefixed `2719-`.
No independent model review is claimed; normal hooks, executable acceptance,
actual required CI, repository permissions and resolved threads govern delivery.
Root separately owns Nix integration, pin and live migration. This source
acceptance is not a live deployment or renamed remote resource claim.
