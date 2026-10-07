# Desktop weekly producer and scoped UI delivery — 07/10/2026

Generator: openai / GPT-6.1 Sol. Existing Impeccable v4.5.0 visual system retained.

## Hypothesis and falsifiers
Desktop weekly quota has no independent producer/reader wiring; Token Plan reports cannot substitute. A bounded real usage read returned 93.6% remaining, reset epoch 1791844029. Falsifiers: independent report produces no Desktop row; stale/missing/ambiguous payload renders fresh; render invokes network; actual `/ui` does not change after the exact build activates.

## Contract
- Separate `THROTTLE_MIMO_DESKTOP_REPORT`; never replace Token Plan's report.
- Atomic 0600 schema-1 JSON; UTC `generatedAt`, 300-second declared interval.
- Exactly one `mimo:desktop-subscription` / `kind=mimo` row, weekly remaining percent. `usedPercent=100-percent`, `remainingPercent=percent`, `unit=percent`; optional measured reset epoch. No monthly credits, inferred allowance/billing/throughput or pace.
- Safe allowlisted unknown report on collection failure, stale after two missed intervals, unknown invalid clocks/rows. No credential or raw exception in output.
- Producer CLI uses the installed bridge's existing account/SSO implementation out of process; no new dependency, no calls in render, no model requests. Exactly one account supported; multiple accounts remain unknown until a per-account contract exists.

## Delivery boundaries
pN owns NixOS-2681; never edit it. A separate NixOS-2683 pin worktree may supply exact source/hash and UI environment. Only `anthropic-throttle-proxy.service` may restart after idle checks; preserve cancelled Anthropic closed-sink and existing lane routing. Keep rollback unit/build rooted. No full switch/reboot or unrelated service restarts. Protected source PR only; Nix unprotected base is a merge blocker. Capture imported/effective/persisted build and real `/ui`, label synthetic vs live.
