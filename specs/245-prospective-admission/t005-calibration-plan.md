# T005 paired calibration — bounded source slice

Base: `b5a08ee05faf83ec2b5d9d19fbbcbd6c0a698790`, fresh worktree/branch
`303-prospective-calibration`. Main WIP, original294/297, candidate2622 and live
mode remain untouched. No provider calls or full desktop test/build execution.

Hypothesis: reuse observe permit's final-body estimate at each real forwarding
attempt to record validated provider usage and independent terminal outcomes,
without altering admission, retry, timeout, response bytes or ledger debt.

## Plan / finite scope

- One bounded helper `prospective_calibration.py` owns parsing AND attempt
  completion, using existing first-1-MiB captures. No daemon, parser service,
  new dependency, settlement, private ledger reader or response-content log.
- Reuse prepared numeric cost on observe permits, including shadow denials/drops.
  Default off and strict create no calibration samples. Unaccountable requests
  retain existing unknown/unbound observation counters, not invented estimates.
- Finish at `_forward_once` and `_forward_once_into_sse`, not logical-request
  `_finalize`; fresh permit and result per retry. Keep synchronous handoff directly
  before transport. Existing `_record_usage`/billing/dashboard parsing unchanged.
- Structured JSON/SSE only. Exact nonnegative integer counts, terminal marker
  validation and monotone cumulative updates (never sum repeated totals).
  OpenAI prompt/cache and completion/reasoning subsets are not double-counted.
  Anthropic full input requires explicit input/cache-read/cache-create fields.
- Missing usage, malformed values, unsupported shape/encoding, cap-hit, missing
  finish, cancellation and transport errors are NOT zero-usage calibration pairs.
  No completion is invented, uncertainty never triggers a retry or timeout raise.
  EOF terminal outcomes are independent of usage comparability; `[DONE]` alone
  is not a finish reason. Capped/encoded bodies cannot prove missing finish.
- Existing public `budget_label` is reused ONLY when exactly one canonical scope
  is configured. With multiple scopes report `ambiguous_scope`, not blended
  statistics. No schema widening: multi-scope per-account/model calibration needs
  a separately reviewed public-alias mapping. Export no account, model, source,
  bearer/hash, URL, request/response content, error text or payload-derived labels.
- Metrics: fixed usage outcome + terminal outcome counter, paired token totals,
  reported-input / estimated-input histogram with bounded static buckets.
  Numeric samples cannot prove vendor rate-window charging, and exclusion of
  capped long responses must remain visible as coverage loss.

## Tasks / acceptance

- [x] Add bounded paired helper, metrics and observe permit context.
- [x] Wire normal and prepared-SSE attempts with exactly-once completion.
- [ ] Synthetic checks: absent/zero, cache semantics, cumulative duplicates,
      malformed/conflicting values, real finish vs empty/missing finish,
      cap/truncation/encoding, cancellation/transport failures, retry isolation,
      single public scope labels and off byte/metric parity.
- [ ] Focused admitted desktop lint/tests only if available; coordinator runs
      full suites/build/coverage and publication acceptance.

## Delivery checkpoint — 04/10/2026

- Preserved seven-file WIP resumed; no existing PR for this branch.
- Focused checks: 74 passed (calibration, runtime, prospective forwarding), 1.28s.
- Ruff formatting repaired; test import ordering repaired with Ruff.
- Current main has seven newer commits through `953cb83`; integration and renewed
  focused acceptance are still pending. Full tests/quality gates remain CI-owned.
- UI/session research remains diagnostic, not a live fix: both previously sampled
  static CSS endpoints returned the same bytes; no existing browser session was
  inspected. Local bearer status and subscription capacity are separate scopes.
  No `/ui` poll or provider request was initiated by that research.

No T005 one-day observation or T006 strict activation is claimed by this slice.
Source metrics only; root separately owns observe configuration. Historical
pH/pK attribution remains unknown; no attribution is inferred from outcomes.
