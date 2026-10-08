# Throttler canonical source identity

08/10/2026. Branch `2719-generic-gateway`; base `515f67d` (merged proxy #332).

## User stories

- P1: Root's Nix adapter installs distribution `throttler-gateway` and runs
  `throttler-gateway` / `throttler-ingress`, using the existing gateway and
  ingress runtime, not a second routing implementation.
- P1: Existing callers retain `anthropic-throttle-proxy`,
  `anthropic-throttle-ingress`, `anthropic_throttle_proxy` imports/module
  execution, configuration, persisted state and wire contracts.
- P2: The dashboard, brand assets and current operational examples identify
  the product as **Throttler**, with truthful provider/protocol/account labels.

## Acceptance

Built wheel and sdist contain canonical and compatibility packages, four real
console scripts, and dashboard assets. Entry points resolve to the same runtime.
Canonical module execution starts the appropriate runtime. Existing routing,
credential policy, anti-spoofing, security and full-suite tests stay green.
Normal hooks, exact-head required CI and zero unresolved review threads precede
a verified squash merge.

## Scope boundary

Source only. Root owns Nix integration, pin and live migration. No live settings,
controls, provider requests, workflows, CODEOWNERS, remote GitHub/Dokku/domain
rename or historical evidence rewrite. Desktop compute uses only
`desktop-job-admit --small`, one thread; refusal is recorded, never bypassed.
