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
- [x] Synthetic checks: absent/zero, cache semantics, cumulative duplicates,
      malformed/conflicting values, real finish vs empty/missing finish,
      cap/truncation/encoding, cancellation/transport failures, retry isolation,
      single public scope labels and off byte/metric parity.
- [x] Focused admitted desktop lint/tests only; full suites/build/coverage run
      in hosted CI. Public PR and live acceptance remain separate gates.

## Delivery checkpoint — 04/10/2026

- Preserved seven-file WIP resumed; no existing PR for this branch.
- Checkpoint `a634c48` preserved the WIP after 74 focused tests passed.
- Merged current main through `953cb83` without conflicts; no source changes to
  other owners' UI, legacy usage/gauge parser, or admission-closure slice.
- Renewed focused acceptance: 132 passed, 13 existing warnings, 13.51s
  (`test_prospective_calibration`, `test_prospective_runtime`,
  `test_prospective_forwarding`, `test_forwarding_paths`, `test_tps_gauge`).
  Ruff check and format-check passed on all six touched Python files.
- Six actual loopback wire checks cover normal and prepared-SSE forwarding in
  off/observe/strict: response bytes and upstream request bytes unchanged; the
  HTTP metrics surface publishes pairs only in observe. No external egress.
  The first off-mode test attempt failed because the fixture accessed observe
  state which correctly does not exist in off; the fixture now drains only observe.
- Alternate cache-hit/miss partitions must conserve full prompt input and agree
  with cached-token details; conflicting totals and in-band SSE errors never pair.
- Published PR #306, head `d1485d0`: hosted full suite **2,040 passed**, 153
  warnings, 83.74s. All eight non-Sonar checks passed. Public GitHub HTML returned
  HTTP 200 and the expected title; PR completion remains pending.
- Sonar exact-head analysis `54ca62d0-6280-4f63-8ab5-791fe6ff4ddd` measured 93.5%
  new-code coverage, 0% new duplication, one issue: S3516 on the always-false
  `CalibrationAttempt.__exit__`. Use the void context-exit contract (`None`), which
  likewise never suppresses exceptions, rather than returning a redundant boolean.
  Renewed hosted validation is required; no rule suppression or threshold change.
- No deployed UI or provider-health success is inferred from synthetic HTTP checks.
- UI/session research remains diagnostic, not a live fix: both previously sampled
  static CSS endpoints returned the same bytes; no existing browser session was
  inspected. Local bearer status and subscription capacity are separate scopes.
  No `/ui` poll or provider request was initiated by that research.

## Source landing

PR #306 merged as `8acd16183114e2352e3d6ceba276c72231432c64` after all nine
hosted checks passed on `ff0cae317782a9cc67c5d1d94c7f8dacb1595a4f`. GitHub's
merge receipt returned `state=MERGED`; no deployment accompanied the merge.

No T005 one-day observation or T006 strict activation is claimed by this slice.
Source metrics only; root separately owns observe configuration. Historical
pH/pK attribution remains unknown; no attribution is inferred from outcomes.
