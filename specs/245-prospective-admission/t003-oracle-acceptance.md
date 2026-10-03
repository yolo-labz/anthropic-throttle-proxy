# Strict acceptance for the spec244 diagnostic

The original spec244 assertions and budgets and its exact-six-failures evidence
test remain intact. Both adapters now share the same loopback provider and
proxy configuration helpers. The new adapter imports the three original
default-off positive controls verbatim. The strict counterpart runs the real handler, limiter,
pacer, forwarder, runtime, owner and persisted ledger against loopback servers.

Only credential discovery is substituted with the fixture's explicit source
label: both declared keys map to one account. Initial routing, post-wait
rerouting, selected metadata and half-open probe ownership all remain real.
The owner's existing clock injection uses the pacer's virtual eight-second
gap; server/deadline clocks remain real. Every experiment stays inside one
60-second ledger window. Egress permits only allocated loopback ports and
records the final serialized bytes before invoking the actual aiohttp transport.
No fixture catches or fabricates a local admission response.

## Fictional budgets and six-case mapping

Output is ALWAYS 60, including each original off Responses request's
`max_output_tokens`. Supported final bodies must account to at most 100 total
input+output units EACH, preventing enlarged inputs from accidentally turning an
RPM-only experiment into a token-denial experiment. No defaults, vendor caps,
plan meters or calibration claims are inferred from these numbers.

| Original red case | Strict owner/resolver budgets (requests, tokens)/60s | Required result |
| --- | --- | --- |
| Two Messages outputs of 60 vs 100 | (2, 100) | One upstream 200, one LOCAL EXHAUSTED 503; peak one |
| Two Chat outputs of 60 vs 100 | (2, 100) | One upstream 200, one LOCAL EXHAUSTED 503; peak one |
| Two Responses outputs of 60 vs 100 | (2, 100) | Zero upstream attempts, two LOCAL UNKNOWN 503s: estimator shape unsupported |
| Two fixture keys sharing one account | (2, 100) | Four clients, one upstream 200, three LOCAL EXHAUSTED 503s; peak one <= original ceiling two |
| Completed requests still debit window | (2, 400) | First two succeed and finish; second wave both LOCAL EXHAUSTED 503; exactly two retained debts |
| First upstream 429, then retry | (1, 200) | Exactly one upstream attempt/retained debt; retry gets LOCAL EXHAUSTED 503 |

The last two are deliberately SEPARATE settings: 400 gives token room for all
four small completed-wave requests; 200 gives token room for both retry attempts.
Thus only their original RPM bounds (two and one) can bind. The token/account
burst cases continue using 100, never a widened token allowance disguised as
acceptance. All origin attempt totals and durable debts are checked against the
SAME Budgets objects supplied to BOTH resolver and persistence owner.

The strict token budget permits only one supported request, so it must NOT inherit
all-200 or peak-two assertions from the off overspend reproduction. Separately,
one supported small request MUST succeed for Messages and Chat; a blanket
fail-closed implementation cannot pass those or the six-case outcome counts.
Responses is explicitly UNSUPPORTED in strict, not claimed to be accounted or
supported. Its unmodified default-off positive control still must succeed.

After drain, persisted canonical key, entry count, token totals, timestamps and
unsettled flags must agree with actual wire attempts, including the real 429.
No missing-usage refund or completed-stream refund can turn the later attempts
green. Unsupported requests must leave no persisted ledger file.

Pacer-invocation times remain `[0,8]` or `[0,8,16,24]`; denied attempts are paced
but do not egress. The adapter does NOT equate those timestamps to actual wire
send instants after asynchronous persistence. Wire and debit timestamps are
independently constrained to the same rolling window.

## Executable acceptance

```sh
uv run pytest -q tests/test_prospective_oracle_live.py tests/test_prospective_admission_evidence.py
```

The adapter has11 passing cases. Its companion diagnostic check requires exactly
six named budget failures and three passes from the original off oracle; an
import/setup error or unrelated failure cannot count as evidence. The complete
suite and integration receipts are in [integration-evidence.md](integration-evidence.md).
This proves configured synthetic budgets, not calibrated vendor token usage or
permission to enable production strict mode.
