# 244 — Prospective admission: evidence before enforcement

Date: 23/09/2026. Baseline: `7ddf462` (#242). **Test/spec slice only.**
No production-source, client, service, routing, credential or capacity changes.

## Problem and claim boundary

A streaming-concurrency ceiling plus a dispatch gap does not specify a token
reservation budget. Credential identity also need not equal a provider account's
quota identity. Headerless 429 alone does not prove either caused the incident.

This slice must establish those gaps with real local HTTP through the shared
proxy before proposing enforcement. No ordinary API number becomes a Token Plan
cap; the test's numbers are explicitly fictional policy, not enabled settings.

## Required future behavior (not implemented)

- One explicit account/model scope shares request and prospective token budgets
  across its keys/callers; independent account scopes remain independent.
- A concurrent burst cannot spend the same available allocation twice.
- Every actual generation attempt, including retries and internal probes, is
  accounted before provider egress. Telemetry and independent providers are not.
- Input, output/reasoning, cache and uncertainty have an explicit cost contract;
  unknown cost never silently becomes zero under enabled strict enforcement.
- Releasing concurrency does not refund rate-window usage. Cancellation, stream
  interruption and restart do not silently restore unspent capacity.
- Default-disabled behavior stays unchanged; no live cap is inferred or enabled.

## Executed evidence

```sh
PYTHONPATH=tests uv run pytest -q -p conftest specs/244-prospective-admission/check_admission.py
```

**6 failed, 3 passed**, repeated identically 20/20 times. Failures are the
specific `BudgetExceeded` assertions, not setup/import/network errors:

| Case | Measured fictional-policy violation |
| --- | --- |
| Messages / Chat Completions / Responses | Two overlapping output bounds of 60 exceed a 100-unit reservation budget; dispatch at virtual t=0,8 |
| Two bearer keys, one fixture-owned account/model | Peak four open streams, despite two slots per bearer |
| Two completed waves | Four requests at t=0,8,16,24 against two/60s |
| One logical call, first upstream 429 | Two actual attempts against one allocation |

Three small-request controls pass. The pacer uses its actual eight-second gap
with a virtual clock; the HTTP handler, limiter, cold probe, streaming and retry
loop remain real. Outbound requests are guarded to the allocated fixture ports
only. Streams are event-held after headers, with no final usage available at the
overlap. Test cleanup asserts zero inflight/queued work.

No skipped/xfail tests are added. Normal CI runs
`tests/test_prospective_admission_evidence.py`, which executes this diagnostic
artifact in an isolated Python process and requires exactly the six named
`BudgetExceeded` failures plus the three passing controls. Import/network/
cleanup failures, unexpected passes and extra failures fail that artifact check.
The explicitly invoked spec remains RED: this is evidence delivery, not six
fixed bugs. A future implementation must configure the fixture budgets in its
opt-in path and replace the diagnostic expectation with enabled-policy
acceptance. Full suite: **1338 passed**, no expected-failure markers; Ruff
clean. See [evidence](evidence/).

## Scope exclusions and blockers

No tokenizer/accounting rule is fabricated. No new wait scheduler, paid route,
key rotation, additional slots, deployment or changes to `clients/`.

Before implementation: settle operator-owned quota scope, local budget/window,
validated input/output estimation and uncertainty/restart policy. Provider
rate-accounted tokens are not monthly credits or discounted billed tokens.
The live 429 cause and Token Plan accounting remain unknown.
