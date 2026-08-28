# Spec: provider-aware subscription dashboard

## Problem

The dashboard consumes the lane report but recognizes only Codex, Copilot, and
balance-shaped lanes. A valid `zai:plan` row would therefore have the wrong
family, no quota windows, no billing state, and no recognizable provider icon.
The screen cannot answer which paid plan is current or when its hard limits
reset.

## Requirements

- Normalize additive `kind=zai` rows with 5h/7d meters, reset epochs, plan,
  billing-current, auto-renew, renewal amount/date, and raw payment type.
- Keep unknown/stale/refused status fail-closed; never infer payment success
  solely from `paymentType=WAIT_PAY`.
- Give every rendered provider kind an accessible emoji plus text: Anthropic,
  Codex, Z.AI, DeepSeek, Copilot, Groq, and DeepInfra.
- Render Z.AI as `✨ Z.AI`, plan/version, `✅ billing current` or an explicit
  warning, `💳 $80/mo`, `🔄 renews 27/09`, and both hard-reset countdowns.
- Continue to render no raw credential, account ID, order number, agreement
  number, or customer ID.
- Preserve the HTMX-only, server-rendered, Catppuccin dashboard invariants.

## Acceptance

- Lane unit tests cover healthy, exhausted, invalid-billing, stale, and unknown
  Z.AI rows.
- Route/template tests prove provider icons, billing text, both quota bars, and
  reset countdowns while rejecting secret-shaped fields.
- Full pytest, ruff check/format, and rendered preview pass.
