# Tasks — default-off integration

No mode is enabled by this source change. T005 requires accepted T001–T004
plus explicit operator authority and complete calibration evidence.

- [x] T000 Audit the callers (inherited from 244) and record the placement list
  this contract must satisfy.
- [x] T001 Token accounting: extract validated final input tokens after body
  shrink and model selection, and the output bound (`max_tokens` else model
  default). Refuse to reserve for an unparsable body.
- [x] T002 Reservation ledger: one process-local ledger per `(upstream, account,
  model)`, 60 s rolling request + token windows, atomic check/debit with no await
  between, debt retention on 429/timeout/missing usage, restart debt, no refund
  for unknown discounts.
- [x] T003 Wire every audited dispatch site: `_forward_once`,
  `_forward_once_into_sse`, `_retry_direct_once`, `_probe_upstream_auth_once`,
  `_credential_recheck_one`, `ingress.handler`. A site that cannot be accounted
  must be explicitly refused under strict mode, not skipped silently.
- [x] T004 Refusal surface: `503` + `Retry-After` + provenance header, excluded
  from AIMD and retry ladders, counted separately, terminal-error channel for a
  post-commit denial.
- [ ] T005 Calibration: flag on, observe-only, budgets above the measured peak,
  one full day of data, budgets then set from p99.
- [ ] T006 Strict mode for the MiMo lane only, with the ten falsifiers green
  (spec §Falsifiers) and default-off byte parity re-proved.

## Acceptance

`uv run pytest -q` includes the strict adapter in
`tests/test_prospective_oracle_live.py`: six original gap scenarios, two supported
strict positive controls and the three unchanged default-off controls. The
original spec244 assertions and budgets remain intact and intentionally red
with six budget failures; `test_prospective_admission_evidence.py` verifies exactly that
result, so default-off cannot falsely claim enforcement. See
`t003-oracle-acceptance.md` and `integration-evidence.md`. Ruff must pass.

## Explicitly out of scope

Provider account discovery, vendor ceiling inference, cache-discount modelling,
cross-process coordination, and any claim that local admission removes vendor
429s.
