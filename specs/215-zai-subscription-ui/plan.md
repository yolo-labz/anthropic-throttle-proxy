# Plan: provider-aware subscription dashboard

## Constitution check

- Dedicated sibling worktree: PASS.
- Read-only lane report consumer; no outbound call or credential handling.
- No hot-path change, SDK, new dependency, JavaScript module, or raw palette.
- One-revert proxy UI slice.

## Design

1. Extend `lanes.py`'s additive report schema with Z.AI family/icon/display and
   generic window normalization.
2. Carry only sanitized billing fields into the view.
3. Add icon/display/billing fields to the unified subscription rows, including
   Anthropic and existing report lanes.
4. Render semantic text alongside every emoji and status color.
5. Add focused unit/template tests, then run the full suite and ruff.

## Falsifiers

- `WAIT_PAY` with an invalid/inactive plan must not render billing current.
- 100% 5h or 7d meter must render exhausted and name the later hard reset.
- Stale report must remain stale even when the embedded plan says VALID.
- Fixture secrets/order/customer identifiers must not enter rendered HTML.
