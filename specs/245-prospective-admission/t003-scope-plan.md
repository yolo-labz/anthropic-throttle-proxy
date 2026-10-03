# T003 foundation — canonical scope authority (plan)

**Status: foundation only.** Pure, validated operator-config resolution of one
dispatch's trusted identity facts to the canonical `(upstream, account, model)`
scope that T002's ledger keys on. No enforcement, no wiring, no strict/observe/
off policy — those belong to the T003 dispatch-site callers and T006.

## What ships here

- `src/anthropic_throttle_proxy/prospective_scope.py` — `Scope`, `ScopeMatch`
  (`scope` + explicit `ledger.Budgets` + optional `output_default`), distinct
  `UnknownScope(reason)`, and `ScopeResolver` over **already-parsed mapping
  rows** (a sequence of row mappings, or a mapping of named rows). No file
  loader, no env, no runtime flags, no I/O, no network, no model rewriting.
- `tests/test_prospective_scope.py` — hermetic positive/negative coverage.

`Budgets` is reused from T002's `ledger` verbatim (positive-int-only,
bools/floats excluded); `output_default` is T001's "model default" output bound
for requests without `max_tokens`, and it is **optional** — absence is carried
through as `None`, never invented.

## Authority model (contract §Authority)

A scope binds from **operator configuration only**:

| Fact | Trusted source | Never from |
|---|---|---|
| credential-source label | the routing decision's configured account metadata (the operator's credential-source labels) | caller headers, bearer hashes (`_bearer_id` identifies a credential, not an account) |
| upstream endpoint | the proxy's configured upstream target, matched **exactly** (a trailing slash is a different endpoint) | request URLs, provider-registry membership |
| model | the wire spelling selecting a **configured** alias | quota percentages; an unconfigured spelling cannot create authority |

**Trusted-callsite requirement.** The future wiring (T003: `_forward_once`,
`_forward_once_into_sse`, `_retry_direct_once`, `_probe_upstream_auth_once`,
`_credential_recheck_one`, `ingress.handler`) must pass the source label from
trusted account metadata resolved at the dispatch decision — scope is resolvable
per dispatch, not per connection — and the endpoint the proxy itself selected.
A caller-supplied value is never an authority input; the model string only
selects among configured aliases and falls to `UnknownScope` otherwise.

## Validation (constructor, deterministic, first failure wins)

Rejects with `ValueError`:

1. ambiguous duplicate `(source, endpoint, alias)` mappings;
2. an alias resolving to more than one canonical model;
3. inconsistent budgets or output defaults for a shared canonical scope — so
   alias and canonical-id spellings resolve to the **same configured default**;
4. invalid types, bools and nonfinite values (exact `str` labels; `Budgets`
   positive ints; `output_default` positive int);
5. rows referencing an unknown credential source, upstream endpoint or model
   relative to the inventories the caller passes.

`resolve()` never raises on dispatch input and never guesses: invalid input or
an unmatched triple returns `UnknownScope` with a coarse reason
(`invalid-input` / `unknown-source` / `unknown-endpoint` / `unknown-model` /
`unmapped`). Strict/observe/off handling of that answer belongs to the future
caller per spec 245 (fail closed only under strict mode).

## Not here (explicitly)

- **Probe carve-out still unwired.** Spec 245 accounts internal probes to their
  own small budget so diagnostics cannot starve the fleet; nothing in this
  module exempts, routes or budgets probes, and no caller exists yet.
- Enforcement, refusal surface (T004), calibration (T005), strict mode (T006),
  token accounting (T001), the ledger itself (T002).
- No vendor limits, provider defaults or account discovery: every budget number
  is operator configuration, and there is no default budget anywhere.

## Validation on landing

`uv run pytest tests/test_prospective_scope.py -q` plus the 244 oracle and the
full suite must pass; ruff clean. (Mac owns test execution and publication for
this slice.)

## Mac executable corrections

Initial focused acceptance was35 pass/1 fail: an allowed-but-unmapped model
was incorrectly expected to be unknown-model. The fixture now uses an actually
unknown model and retains the separate known/unmapped control. A second red
regression showed global alias uniqueness incorrectly rejects a common default
alias at two distinct trusted provider endpoints. Authority is the full
(source,endpoint,alias) triple; its existing duplicate check is sufficient.
Removing the redundant global alias constraint permits that positive case while
shared canonical budget/default consistency and ambiguous-triple rejection remain.

Independent Astra review found three additional concrete configuration defects:
canonical alias chains/cycles, splitting one selected canonical model into
different account scopes, and whitespace-only ledger keys. All six new
regression cases were RED before correction. Canonical authority now uses
(source,endpoint,canonical-model); remapped targets and conflicting full scopes
refuse at construction, and blank strings refuse without normalizing valid IDs.

Final Mac acceptance:42 focused cases and1,759 full tests pass; module
statement/branch coverage both100%; Ruff passes. CI remains an exact-head gate.
