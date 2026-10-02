# Viewport-fit dashboard — 01/10/2026

## Hypothesis

The dashboard scrolls because the gauge occupies a half-width grid row alone,
telemetry stacks vertically, and repeated provenance/absolute timestamps inflate
nine capacity rows. Reflow and progressive disclosure can fit the overview without
shrinking the type scale or clipping operational facts.

## Acceptance

- [x] Nine-seat overview, gauge, golden signals, routing, and footer fit at
  1366×768, 1440×900, 1920×1080 and 2210×1240 without document or panel scrolling.
- [x] Keep every seat, meter percentage, remaining allowance, relative reset,
  warning, and transport/capacity/eligibility distinction in the overview.
- [x] A keyboard-accessible details toggle exposes provenance, absolute reset
  timestamps and repeated binding-window summaries; it survives simulated stats
  fragment replacement. Actual live polling remains an activation check.
- [x] Preserve the type scale and normal document flow: no zoom, transforms,
  fixed-height clipping, or overflow-hidden on the document. Extra rows, expanded
  details and zoomed/mobile views may naturally scroll rather than lose content.
- [x] Mobile has no horizontal document overflow; existing semantics/tests pass.
- [x] Exercise the actual renderer in isolated Chromium; 1478 pytest cases and
  Ruff pass. The local pytest timeout was superseded by isolated server execution.
- [ ] Complete normal commit hooks and protected PR CI/merge. Blocked by local
  gate stalls and subsequent SSH unreachability of the alternate execution host.
- [ ] Activate and verify only the idle dashboard, not the busy MiMo lane.

## Scope / reversal

Presentation only; no routing, quota, credentials, service limits or model changes.
Use existing HTMX and native HTML/CSS, no dependencies or additional JavaScript.
One revert PR plus restoration of the rooted dashboard unit reverses the slice.
The previous recovery/input-guard work remains separate and must not be declared
complete by this layout change.
