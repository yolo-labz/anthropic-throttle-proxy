# Attempt correlation on usage-ledger rows

Opt-in, content-free, measurement only. `THROTTLE_USAGE_LEDGER=observe` **and**
`THROTTLE_ATTEMPT_ID_CAPTURE=observe` (default `off`; any other value is off). With it off, rows
and the hot path are unchanged (two no-op calls per upstream send).

## Units

| Unit | Meaning |
|---|---|
| gateway request | one client request to the gateway. One ledger row. `gateway_request_id` is a random hex made here. |
| physical attempt | one upstream HTTP send for that request. `physical_attempts` counts them (held 429 retry, direct fallback). |
| provider request | the upstream's own id (`request-id`, else `x-request-id`), kept only when it is `req_` plus 8 to 64 alphanumerics. |

## Added row fields

`gateway_request_id`, `attempt_binding`, `x_pi_attempt_id`, `physical_attempts`,
`upstream_responses`, `retry_observed`, `provider_request_ids` (one per response, `null` where
absent or malformed, at most 8), `provider_request_id`.

`attempt_binding` is how the client's `x-pi-attempt-id` claim ended:

| Value | Rule | `x_pi_attempt_id` |
|---|---|---|
| `claimed` | one header, canonical lower-case UUID v4, first seen in this process window | the value |
| `missing` | no header | null |
| `invalid` | anything else (case, spaces, newline, other UUID versions, long) | null, value never stored |
| `repeated` | the header twice, same value | null |
| `conflicting` | the header twice, different values | null |
| `replayed` | a valid id already seen (bounded window of 8192, in memory) | null |

`provider_request_id` is non-null only for exactly one send, one response and direct egress.
Retry, a send without a response, or a central relay leave it null; the list keeps the evidence.

## What the gateway does not do

* It does not authenticate the producer. `claimed` is a **claim**.
* It does not infer the account, a reset or a window from time or from the selected model.
  `bearer` stays the existing opaque hash; no reset field is added.
* It does not record the header set or any body.
* Provider ids on error responses (429, 5xx) are kept as well; the proxy-generated refusals
  (queue timeout, local admission) have no upstream response, so `upstream_responses` is 0.

## Join rule for a consumer (DE)

Join a producer row (Pi `usage.jsonl`, `attemptBinding = "bound"`) to a gateway row only when
**all** hold: the gateway row is `claimed`; both sides carry the same attempt id; both sides carry
the same non-null `providerRequestId` / `provider_request_id`; the id appears in exactly one row on
each side; `retry_observed` is false. Otherwise the pair is request-only on each side: no join.
A replay cannot rewrite the first row, so an id seen in two rows joins nothing.

## Limits

* The seen-id window is in memory. A restart forgets it; the same id after a restart is `claimed` again.
* Provider ids from the stub paths in tests are synthetic. Real `request-id` shape is Anthropic's
  `req_...`; a different shape is dropped, not guessed.
* Natural evidence needs the producer opt-in (`PI_ATTEMPT_ID_HEADER=1`, NixOS #4199) and this flag.
