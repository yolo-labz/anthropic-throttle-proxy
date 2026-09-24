# Tasks — contract slice

Contract only. Nothing here turns on enforcement; T005 is the first task that
may, and it is gated on T001–T004 landing and being measured.

- [x] T000 Audit the callers (inherited from 244) and record the placement list
  this contract must satisfy.
- [ ] T001 Token accounting: extract validated final input tokens after body
  shrink and model selection, and the output bound (`max_tokens` else model
  default). Refuse to reserve for an unparsable body.
- [ ] T002 Reservation ledger: one process-local ledger per `(upstream, account,
  model)`, 60 s rolling request + token windows, atomic check/debit with no await
  between, debt retention on 429/timeout/missing usage, restart debt, no refund
  for unknown discounts.
- [ ] T003 Wire every audited dispatch site: `_forward_once`,
  `_forward_once_into_sse`, `_retry_direct_once`, `_probe_upstream_auth_once`,
  `_credential_recheck_one`, `ingress.handler`. A site that cannot be accounted
  must be explicitly refused under strict mode, not skipped silently.
- [ ] T004 Refusal surface: `503` + `Retry-After` + provenance header, excluded
  from AIMD and retry ladders, counted separately, terminal-error channel for a
  post-commit denial.
- [ ] T005 Calibration: flag on, observe-only, budgets above the measured peak,
  one full day of data, budgets then set from p99.
- [ ] T006 Strict mode for the MiMo lane only, with the ten falsifiers green
  (spec §Falsifiers) and default-off byte parity re-proved.

## Acceptance

`uv run pytest -q` plus the 244 oracle (`PYTHONPATH=tests uv run pytest -q -p
conftest specs/244-prospective-admission/check_admission.py`) must both pass, and
the oracle's six budget failures must flip to green **without** weakening a
positive control. Ruff clean.

## Explicitly out of scope

Provider account discovery, vendor ceiling inference, cache-discount modelling,
cross-process coordination, and any claim that local admission removes vendor
429s.
