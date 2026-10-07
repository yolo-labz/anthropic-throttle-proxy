# Independent browser acceptance — 07/10/2026

## Scope and hypothesis

Own executable acceptance and sanitized receipts only. Base: PR #316,
`66ad9dc9df82887bccc90572ae0d84dadf5df5fe`. No template/backend/runtime edits,
new dependencies, agent launches, supervised profiles or production mutations.

Hypothesis: exact #316 preserves per-tab source selection and focused regions
through actual HTMX swaps while distinguishing measured, stale and unknown
throughput without collector network calls during render.

Falsifiers: a poll changes source/focus, cold/stale/error data becomes numeric
zero, a cached render calls a collector, or narrow layout overflows. Browser
refusal is a blocker, not passing evidence. Synthetic acceptance is not live
post-deployment acceptance.

## Plan

1. Read versioned Impeccable 4.5.0 (engine 0.1.11), canonical browser INDEX and
   Cookbook. Reuse delivery-group Playwright and existing gauge check.
2. Capture old live desktop source/build and sanitized DOM/API summaries.
3. Extend the existing executable check with viewport, keyboard/focus,
   state-transition, console/network and sanitized screenshot/DOM receipts.
   One ephemeral headless context, sequential 1366 and 390 CSS-pixel viewports;
   GPU disabled. No account/profile attach or external API calls.
4. Run only through heavy admission. Bound initial pass plus one confirmation;
   never use the small control lane for tests/rendering after heavy refusal.
5. Normal hooks/PR/CI; record exact checks and deployment drift/blocker. Runtime
   owner alone changes services. Compare latest sibling commits without edits.

## Constitution check

No vendor SDK, bearer disclosure, limiter changes, added JS or provider fallback.
Existing installed platform tools before dependencies. Durable artifacts live in
`docs/evidence/ui-browser-qa-2026-10-07/`; report is
`docs/UI-BROWSER-QA-2026-10-07.md`.
