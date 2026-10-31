# Plan

Hypothesis: the existing score has no account-rotation state or per-account work accounting, so idle sequential requests can remain sticky even with two eligible accounts. Falsifier: the real routing seam already alternates equal-work requests and compensates unequal measured work without new state.

Use one small account-pool tracker keyed by configured labels, not rotating bearer tokens. Selection remains a pure read; only real route selection advances the tie-breaker. Reserve mean observed token work per transport attempt, reconcile parsed usage, retain marked estimates for unknown sent outcomes, and roll back local admission refusals. Rebase newly eligible accounts to the active virtual-work floor so quota absence earns no catch-up credit. Reuse existing hard eligibility gates and existing HTMX table/styles; no dependency or vendor SDK.

Touch config mode parsing, shared account-selection seam, actual forwarding-attempt accounting and UI collection/template/projection. Add focused scheduler/router/render tests and document mode semantics. Preserve legacy modes, central/direct provenance, explicit API-key handling, AIMD, quota persistence and statusline selection parity. No private account evidence enters public review packets. Runtime/identity/usage receipts stay in the vault.

Isolation: sibling feature worktree because shared config/metrics files carry another session's WIP. Native plan/tasks/implementation flow used: the installed Workflow adapter is Opus-pinned and unavailable here. Public GLM research batch was blocked before spawn; no retry/fallback launched.
