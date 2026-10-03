# T003/T004 application lifecycle integration

Hypothesis: the dispatch seams need one explicitly configured, application-owned
runtime, started before background probes and closed after request/probe contexts.
Without that attachment, helper tests can pass while production always uses off.

This Mac-owned slice follows the existing 297 runtime and 298–301 dispatch seams.
It does not change the merged Nix2609 pin, enable any live admission mode, alter
provider registry policy, or invent account budgets/default model bounds.

## Plan and bounded tasks

- [x] Reuse 297's exact application/request accessors after its source freezes.
- [x] Default mode off ignores all optional manifest paths and creates no owner,
      worker, ledger, file reads or response headers.
- [x] Enabled mode requires explicit operator configuration: trusted source,
      endpoint and model catalogs; canonical scope/budgets/output default; unique
      state-file custody and explicit cold-start permission; bounded pending and
      wait settings; a public budget label and local retry hint.
- [x] Parse a bounded configuration once at startup; reject unknown keys, duplicate
      keys, malformed values, mode/state-file mismatch and ambiguous custody.
      No hot mode switches, watchers, retries or automatic ledger deletion.
- [x] Start before auth/recheck tasks; close only after those contexts and request
      handlers drain. A close timeout remains an error, never a custody-transfer
      receipt. Keep state available for the eventual process restart recovery.
- [x] Attach the same lifecycle to ingress, whose unsupported strict relay topology
      refuses honestly rather than double-debiting a downstream-selected account.
- [x] Expose fixed-reason local refusal/observation metrics; never account, token,
      endpoint or request labels. Preserve normal provider feedback controls.
- [x] Run meaningful off/no-file and strict startup/custody/restart fixtures, then
      the combined exact-source suite and existing spec244/245 falsifiers.

Observe samples are incomplete when dropped/refused, and cannot by themselves
satisfy T005's complete measured-demand/estimator-error calibration evidence.
T005 still requires one full day after accepted code and an explicitly reviewed
scope configuration. Strict deployment remains gated on that measured evidence.

The concrete schema and restart/custody rules are in [runtime-config.md](runtime-config.md).
