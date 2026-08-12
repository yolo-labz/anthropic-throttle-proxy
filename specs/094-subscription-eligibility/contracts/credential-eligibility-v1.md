# Ingress Credential Eligibility Contract — ADR-6a (`adr6a-credential-mode/1`)

**Status**: FROZEN for implementation (`/tmp/fleet-foundry-adr6a-interface.md`, 12/08/2026).
**Repo**: `yolo-labz/anthropic-throttle-proxy` (`:8760` ingress).

## 1. Request gate (opt-in)

```http
X-Anthropic-Throttle-Require-Credential-Mode: subscription
```

(Actual header name: `x-anthropic-throttle-require-credential-mode`.)

- **Absent** → today's behavior, unchanged. No existing fleet traffic changes.
- **Present** → the ingress restricts the role chain to eligible lanes only;
  if none can serve, it **refuses** (§3) and **does not spill**.
- Any other value, or a duplicated header → `400` fail-closed
  (`error.type == "unsupported_credential_requirement"`), before body read,
  lane selection, or egress.
- The header is consumed by ingress and never forwarded.

The fail-open hazard is closed on the caller side: an un-upgraded proxy ignores
an unknown request header, so a caller must treat a response as trustworthy
only with the response stamp (§2.1) AND the capability declaration (§2.2).
Absence of either is refusal, not "probably fine".

## 2. Attestation shape

### 2.1 Response stamp — on CONSTRAINED responses only (r1/C3)

```http
X-Anthropic-Throttle-Credential-Mode: subscription | proxy_key | direct_key | unknown
```

Alongside the existing `x-anthropic-throttle-lane` and `x-anthropic-throttle-role`.

r1/C3 (fleet-orch ruling): stamp ONLY responses to requests carrying
`x-anthropic-throttle-require-credential-mode: subscription`. Unconstrained
legacy traffic is behaviorally unchanged (Pedro 12:17 boundary correction) —
no stamp, no behavior delta. §1.4/§6.1 stand unamended.

| Value | Meaning | Where it appears |
|---|---|---|
| `subscription` | CLASS ∧ CAPACITY verified for this request, fresh per §1.3 | constrained 2xx response · health |
| `direct_key` | lane holds a provider API key (E1 false) | **health only** |
| `proxy_key` | proxy-held credential that is not a subscription plan, or cannot be shown to be one | **health only** |
| `unknown` | eligibility could not be determined | constrained **403** (§3) · health |

Consequence of C3: on the *response* the enum collapses to `subscription` on a
served 2xx and `unknown` on a 403 refusal — `direct_key`/`proxy_key` are
unreachable there by construction. The full four-value vocabulary is normative
for the per-lane health field (§2.2).

**Anti-spoof:** the ingress strips any inbound credential-mode/-reason header
from the client request before forwarding, and strips same-named headers from
every upstream response. Only the ingress authors the truth.

### 2.2 Capability declaration — on ingress health

`GET :8760/__throttle/health` gains a top-level object:

```json
"enforcement": {
  "credential_mode": true,
  "contract": "adr6a-credential-mode/1",
  "subscription_upstreams_count": 1,
  "subscription_upstreams_digest": "a3f1c2b40de7"
}
```

r1/C6: count + digest, never the member list. Digest = sha256 over lowercased
sorted comma-joined hosts (UTF-8), lowercase hex truncated to 12; empty
allowlist ⇒ count 0, digest `""` (the visibly fail-closed state).

and each lane object gains `credential_mode` (+ `credential_mode_reason` when
`unknown`) beside `{open, detail, checked_ago_s}`. Bounded O(lanes); no
per-bearer or per-client detail (AC-25).

### 2.3 What a consumer must check to trust a receipt

All four, in order; any failure ⇒ untrusted:

1. `enforcement.credential_mode == true` **and** `enforcement.contract` is a
   known version;
2. response carries `x-anthropic-throttle-credential-mode: subscription`;
3. response carries `x-anthropic-throttle-lane`, and that lane is in the
   consumer's own allowlist (AC-04b);
4. the stamp arrived on the response to **this** request — never carried over
   from a cached probe.

Transport-trusted, not cryptographic (PO OQ-6: signing optional under
trusted-worker v0).

## 3. Refusal path

When a request demands `subscription` and no eligible lane can serve its role:

```http
HTTP/1.1 403 Forbidden
X-Anthropic-Throttle-Credential-Mode: unknown
X-Anthropic-Throttle-Refusal: no_eligible_lane | eligible_lanes_exhausted
Content-Type: application/json

{"type":"error","error":{"type":"no_eligible_lane",
 "message":"no lane satisfies credential_mode=subscription for role=generate",
 "eligible_configured":1,"eligible_open":0,"reset_hint_epoch":1786503034}}
```

Three properties:

1. **Pre-call.** Refusal happens at selection, before any upstream dispatch.
   Falsifier: the ineligible lane's `served` counter is unchanged.
2. **`403`, not `503`.** `503` is capacity pushback (feeds `_should_retry_pushback`,
   AIMD shrink, client retry). An eligibility refusal is a **policy** verdict —
   retrying walks into the same wall and would shrink a healthy lane for a
   reason unrelated to load. `403` keeps policy and capacity in separate
   channels.
3. **Two distinguishable reasons:**
   - `no_eligible_lane` — no lane of the required class is configured or
     determinable → operator fixes config/adapter;
   - `eligible_lanes_exhausted` — an eligible lane exists but is capped/paused →
     operator waits for reset (`reset_hint_epoch` when known).

A refusal is never a `200` with an empty body, an empty completion, or a
silent fallback to an ineligible lane.

## 4. The predicate — positive allowlist, three signals, all required

A lane is **eligible** for a controlled workload iff all three hold:

| # | Signal | Source | Eligible value |
|---|---|---|---|
| **E1** | `api_key.enabled` | lane `/__throttle/health` | `false` — the lane does **not** hold a direct provider API key |
| **E2** | `upstream` | lane `/__throttle/health` | a CANONICAL upstream whose host ∈ `INGRESS_SUBSCRIPTION_UPSTREAMS` — scheme `https`, host ∈ allowlist, port ∈ {absent,443}, path ∈ {"","/"}, no userinfo/query/fragment (r1/C4) |
| **E3** | subscription-window presence | lane health per-bearer `unified` | **≥1 usable bearer** carrying 5h/7d window state (CAPACITY; freshness per §1.3) |
| **E4** | desktop locality (r1/C2) | lane URL + health `central_url` | lane URL host is **loopback** ∧ `central_url == ""` |

**Two levels (r1/C1).** CLASS = E1 ∧ E2 ∧ E4 governs the stamp, per-lane health
`credential_mode`, and `eligible_configured`. CAPACITY = E3 governs selection
and `eligible_open`. A lane is selectable for a constrained request iff
CLASS ∧ CAPACITY.

`detail` strings, lane names, and credential custody are never inputs.

**Config, fail-closed:** `INGRESS_SUBSCRIPTION_UPSTREAMS` unset or empty ⇒
**no lane is eligible**, and a controlled request refuses per §3. An empty
allowlist must never mean "allow all".

**Per-request, not per-lane-report:** the predicate is evaluated at selection
time, for the request being served. Credential class (E1/E2) may come from the
poll cache; usability (E3) is re-verified by a fresh health probe of the
candidate lane at constrained selection time (ADR-6a §1.3). The stamp on a
successful response is therefore attributable to that request.

## 5. Unconstrained compatibility

Omit the request header to retain existing selection, pinning, spill, retry,
and response behavior. The only visible change is the response stamp on
CONSTRAINED requests, the per-lane `credential_mode` health fields, and the
stripping of spoofed reserved headers.

## 6. Capability upgrade contract

Old ingress: no `enforcement` object → compliant consumer sends no model
request. Upgraded ingress: `contract` version string; unknown version ⇒
consumer refuses fail-closed.
