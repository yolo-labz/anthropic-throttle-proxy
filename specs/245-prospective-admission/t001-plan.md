# Prospective accounting foundation — 03/10/2026

## Scope and plan

Spec245 T001 calls for validated final-input demand and an output bound. This
slice provides a default-off helper for supported text and function-call
requests. It does not complete the prospective-admission program or establish
calibrated vendor usage. No forwarding or ingress path imports the helper.

Reuse an existing estimator if present; a source search found none. Parse the
final bytes after model/body changes; reject unsupported input. Count the
serialized input, including tool definitions, at full weight without assumed
cache discounts. Use a valid explicit `max_tokens`, or an operator-provided
model default only when that key is absent. Return `None` for unavailable
accounting. The byte/4 estimate must be calibrated under T005 before strict
admission; no model defaults or vendor budgets are invented.

## Implemented acceptance

- [x] Frozen owner release transported without editing its original worktree.
- [x] Missing-module RED reproduced before implementation.
- [x] Text and complete function-call shapes; tool definitions affect demand.
- [x] Remote image/audio/document references and unsupported output/endpoint
  formats refuse rather than estimating their actual cost from URL bytes.
- [x] Missing, malformed and explicit-null bounds refuse; only absence defaults.
- [x] Malformed message roles, NaN/Infinity and finite-literal overflow refuse.
- [x] Forty-one focused tests pass; Ruff check/format and zero exact clones.
- [x] Full suite: 1,591 tests pass.
- [ ] Exact-head CI.

The Chinese-frontier pG authored the initial three-file slice. Independent
OpenAI review found omitted tool definitions, incomplete message acceptance,
null fallback and non-finite JSON. The owner corrected those, then Mac
integration added the supported-text boundary and executable media, malformed
role and overflow falsifiers. This worktree288 starts from merged proxy287;
all original owner work is preserved.

## Remaining program gates

T002 needs the account/model ledger and durable debt; T003 must cover every
real dispatch, retry and probe; T004 needs truthful refusal/provenance; T005
requires one full day of observe-only calibration; T006 needs bounded strict
acceptance. Neither this helper nor merged contract PR266 satisfies those.

## Measured review follow-up — 03/10/2026

Sonar identified account_request cognitive complexity 23 (limit15); coverage
92.4% and duplication0% already passed. Separate endpoint/shape validation from
accounting without changing bounds or estimator behavior. Cross-family review
of the OpenAI boundary corrections also found permissive tool definitions;
restrict definitions to explicit named function schemas, with five refusal
fixtures. The focused slice now has46 passing tests. No runtime caller added.
