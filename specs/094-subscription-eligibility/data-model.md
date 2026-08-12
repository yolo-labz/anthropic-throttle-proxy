# Data Model: Subscription Eligibility v1 (ADR-6a)

## CredentialRequirement

Request-scoped precondition interpreted only by ingress.

| Field | Type | Rule |
|---|---|---|
| header | `x-anthropic-throttle-require-credential-mode` | one value, exactly `subscription` |
| mode | literal `subscription` | contract supports no other mode |

State: absent (unconstrained) or valid `subscription`. There is no partially
valid state; duplicate/malformed values are rejected with `400` before body
read/selection.

## CredentialMode

Immutable per-lane classification derived from trusted lane health:

| Value | Meaning | Predicate (CLASS = E1∧E2∧E4) |
|---|---|---|
| `subscription` | CLASS = E1∧E2∧E4 | `api_key.enabled=false` ∧ canonical upstream ∈ allowlist ∧ loopback ∧ `central_url==""` |
| `direct_key` | lane holds a provider API key | E1 fails |
| `proxy_key` | proxy-held credential not proven subscription | E2 fails (upstream not allowlisted) |
| `unknown` | eligibility not determinable | evidence missing/malformed |

`unknown` is explicit, never an absent field, and always paired with a stable
`credential_mode_reason` token.

## LaneState (extended)

| Field | Type | Rule |
|---|---|---|
| `open` | bool | unchanged availability |
| `checked_at` | float | unchanged |
| `detail` | str | unchanged |
| `credential_mode` | str | `subscription`/`direct_key`/`proxy_key`/`unknown`, from poll |
| `credential_mode_reason` | str | stable token when `unknown` |
| `credential_reset_at` | epoch or null | optional refusal hint (only when every bearer budget-blocked) |

## LaneState transition rules

| Event | Availability change | Credential fields |
|---|---|---|
| Successful health poll | Replace from current health | Recompute mode/reason/reset |
| Failed/timeout/oversized poll | Close lane | Mode `unknown` + reason (`health-404`/`unreachable`/…) |
| Request-time saturation | Close lane temporarily | Preserve last polled class |
| Same-lane retry | Reopen availability temporarily | Preserve last polled class |
| Constrained request fresh probe | Replace from fresh health | Recompute (per-request attribution) |

## PolicyRefusal (ADR-6a §3)

Stable local `403` response produced before ineligible model egress:

| Field | Type | Meaning |
|---|---|---|
| `type` | `"error"` | envelope |
| `error.type` | `no_eligible_lane` / `eligible_lanes_exhausted` | classification vs capacity |
| `error.eligible_configured` | int | lanes of the required class |
| `error.eligible_open` | int | eligible AND open |
| `error.reset_hint_epoch` | epoch, optional | trusted earliest reopening candidate |
| header `x-anthropic-throttle-refusal` | same token | machine-readable |

`403` keeps policy out of the capacity/AIMD/retry channels that own `503`.

## CredentialAttestation

Ingress-authored response headers on **every** forwarded response:

| Header | Value |
|---|---|
| `x-anthropic-throttle-credential-mode` | actual mode of serving lane |
| `x-anthropic-throttle-credential-mode-reason` | stable token, only when `unknown` |
| `x-anthropic-throttle-lane` / `-role` | existing evidence |

Reserved credential headers received from client or lane are deleted before
this state is created.

## Enforcement capability (ingress health)

```json
"enforcement": {
  "credential_mode": true,
  "contract": "adr6a-credential-mode/1",
  "subscription_upstreams_count": 1,
  "subscription_upstreams_digest": "a3f1c2b40de7"
}
```

Per-lane health adds `credential_mode` (+ `credential_mode_reason` when
`unknown`) beside `{open, detail, checked_ago_s}`. O(lanes), never per-bearer.
