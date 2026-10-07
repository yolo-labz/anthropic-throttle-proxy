# Gauge caption contrast — 07/10/2026

## Scope / operator story
Frontend-only repair against merged gauge PR #316 (`66ad9dc9`). The operator must be able to read the gauge caption's freshness, unknown-versus-zero and quota-versus-throughput meaning. `.tps-foot` currently uses `--ctp-overlay0` (normal-size `--fs-meta`) on `.tps-panel`'s `--panel`/mantle: 3.5943:1, below WCAG normal-text AA 4.5:1. Existing contrast arithmetic omits this selector.

## Hypothesis / falsifiers
Hypothesis: this caption's low contrast comes from its decorative foreground token, and retokening to existing `--muted` repairs the actual selector/surface pair without changing layout or gauge semantics. Falsifiers: selector-derived foreground/panel background already resolve to ≥4.5:1, or the chosen text token fails on that surface. This source defect does not require browser reproduction before editing (Pedro, 07/10/2026 17:53 BRT); rendered acceptance is separately pending.

## Acceptance
- Add a red-capable regression to `tests/test_ui_contrast.py` that reads the actual `.tps-foot` colour and `.tps-panel` background, resolves their CSS aliases and gates the measured pair at ≥4.5:1.
- Before retokening, that new assertion fails at 3.5943:1; after, it passes at 7.8856:1 with existing `--muted`. Run admitted targeted/full pytest and Ruff, normal hooks and exact-head PR/CI.
- One CSS token replacement only; preserve decorative tokens, large unknown readout, HTMX per-tab state, freshness/unit text, no network in rendering, Catppuccin palette and native server-rendered stack.
- Source arithmetic/tests are not computed-browser contrast, deployment or live accessibility acceptance. Keep those pending in the durable report.

## Non-goals / optional later debt
No backend, collectors, producer/credentials, routing, runtime, deployment, JS/dependencies or palette changes. Prior `specs/317-ui-operability/optional-source-navigation.py` is an unexecuted optional navigation probe, not acceptance of this fix and not a required source delivery gate. No template/navigation changes in this slice. QA/runtime owned files are untouched.
