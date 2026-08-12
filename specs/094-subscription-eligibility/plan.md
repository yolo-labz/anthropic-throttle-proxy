# Implementation Plan: Subscription Eligibility Enforcement and Attestation

**Branch**: `094-subscription-eligibility` | **Date**: 12/08/2026 | **Spec**: [spec.md](spec.md)
**Input**: Fleet Foundry K3 / ADR-6a frozen interface (`/tmp/fleet-foundry-adr6a-interface.md` §0–§6)

## Summary

Extend the existing `:8760` ingress with one opt-in credential requirement.
The ingress derives a fixed per-lane credential class from each configured
lane's trusted health response, filters every selection path before egress,
returns a stable 403 policy refusal instead of spilling, and stamps every
forwarded response with the actual credential class. Unconstrained requests
keep the existing router behavior.

## Technical Context

**Language/Version**: Python 3.13+
**Primary Dependencies**: Python standard library, existing `aiohttp`, existing `prometheus-client`
**Storage**: Process-local bounded lane-state cache; no persistence
**Testing**: `pytest`, `pytest-asyncio`, `ruff`, report-only `mypy` parity, wheel and Docker builds
**Target Platform**: Linux desktop-local aiohttp ingress on `127.0.0.1:8760`
**Project Type**: Existing single Python web service / reverse proxy
**Performance Goals**: Ingress health <50 ms and bounded size
**Constraints**: No model canary, no production/Nix/Dokku mutation, no vendor SDK, no credential logging, no per-client health map
**Scale/Scope**: Fixed configured lane set (currently five live lanes)

## Constitution Check

### Pre-design gate

- **I — No vendor AI SDK**: PASS. Reuse stdlib + existing aiohttp only.
- **II — Bearer identity never a secret**: PASS. No auth header value is retained or emitted; only bounded mode/reason scalars.
- **III — AIMD floor**: PASS / untouched. Policy refusals are 403 and never enter AIMD/retry.
- **IV — Health local and cheap**: PASS by fixed-cardinality summaries, no I/O in `_health`, explicit size/latency oracle.
- **V — Single routing source**: PASS. Enforcement is added to the existing ingress selection point; no upstream or routing shim is added.
- **Workflow**: PASS. Numbered worktree, tests first, full verification, different-family final review, PR/squash merge, no deployment.

### Post-design gate

PASS. The design changes `ingress.py` (the enforcement/attestation), `routing.py`
(one dataclass extension), mirrored tests, and contract/docs. It does not alter
limiter/AIMD state, model forwarding, Nix, Dokku, credentials, or production
configuration.

## Design

### 1. Exact wire contract (ADR-6a frozen)

- Request: `x-anthropic-throttle-require-credential-mode: subscription` (opt-in;
  absent = legacy; invalid/duplicated = 400 fail-closed).
- Response (CONSTRAINED only, r1/C3):
  - `x-anthropic-throttle-credential-mode: subscription` on a served 2xx;
    `unknown` on the 403 refusal (enum collapses by construction; the full
    four-value vocabulary is normative for per-lane health only).
- Refusal (pre-egress):
  - `403` + `x-anthropic-throttle-refusal: no_eligible_lane|eligible_lanes_exhausted`
  - body: `{"type":"error","error":{"type":..., "message":..., "eligible_configured":N,
    "eligible_open":M, "reset_hint_epoch":...}}`
- Health capability (r1/C6):
  - `"enforcement": {"credential_mode": true, "contract": "adr6a-credential-mode/1",
    "subscription_upstreams_count": N, "subscription_upstreams_digest": "…"}` —
    count + sha256[:12] digest, never the member list.
  - per-lane `credential_mode` (+ `credential_mode_reason` when unknown).
- Reserved headers are stripped from client requests and upstream responses.

### 2. Positive credential classification (E1 ∧ E2 ∧ E3)

Per lane health:

- **E1** `api_key.enabled` is exactly `false`.
- **E2** health `upstream` is a CANONICAL allowlisted URL — scheme https, host
  ∈ `INGRESS_SUBSCRIPTION_UPSTREAMS`, port ∈ {absent,443}, path ∈ {"","/"},
  no userinfo/query/fragment (r1/C4; empty allowlist ⇒ no lane eligible).
- **E3** ≥1 usable bearer carrying 5h/7d window state (CAPACITY; freshness per
  §1.3 — a stale `unified_at` sample cannot grant capacity).
- **E4** lane URL host is loopback ∧ health `central_url == ""` (r1/C2).

Two-level classification (r1/C1):
- CLASS = E1 ∧ E2 ∧ E4 → per-lane health `credential_mode` + `eligible_configured`:
  - `subscription` / `direct_key` (E1 fails) / `proxy_key` (E2 or E4 fails) /
    `unknown` (evidence unreadable), each with a stable reason.
- CAPACITY = E3 → selection + `eligible_open`. A capped subscription lane keeps
  CLASS `subscription` so `eligible_lanes_exhausted` stays reachable.

`detail`, lane id/name, and custody are never inputs.

### 3. Selection enforcement

- Constrained requests skip session pins (never honored/overwritten) and walk
  the role chain with a fresh health probe of each candidate lane at selection
  time (ADR-6a §1.3 per-request attribution).
- A candidate is eligible iff its fresh state is `credential_mode=subscription`
  AND open.
- Every selection path (initial, spill, retry re-selection) reapplies the
  requirement; transport errors and bare pushback remain request-local.
- When no constrained lane can be selected: `403 _policy_refusal` with the two
  distinct reasons and optional reset hint.

### 4. Response stamping

- Reserved credential headers are unconditionally excluded while building
  `out_headers` from every upstream response.
- The ingress stamps `credential-mode: subscription` on constrained 2xx
  responses only (r1/C3); the refusal carries `unknown`. The stamp is derived
  from `lane_state` refreshed by the per-request probe on constrained requests,
  so it is attributable to the request served.

### 5. Bounded health

Ingress health advertises the fixed enforcement capability and, for each
configured lane, adds only scalar credential fields. It never copies source
`bearers`, clients, headers, or raw health.

### 6. Compatibility

Header absence takes the current code path. The only unconstrained-visible
change is the credential-mode stamp on every response and removal of newly
reserved attestation headers if a lane attempts to spoof them.

## Threat / Failure Trace

| Threat or failure | Prevention | Executable falsifier |
|---|---|---|
| Direct-key lane reports `detail=ok` | Classifier requires E1/E2/E3; selector filters before call | Healthy direct-key fake is a fail-on-call trap |
| Old ingress ignores header | Mandatory health capability discovery | Health without enforcement object prevents caller dispatch |
| Unknown request semantics | Strict single-value parser | v2/malformed/duplicate header produces zero lane calls |
| Existing direct-lane session pin | Requirement reapplied without replacing shared pin | Pre-pin direct lane; constrained request never calls it |
| Eligible lane saturates then spills direct | Requirement passed to spill selection | Subscription 403 + direct trap yields stable blocked response |
| Lane spoofs positive response stamp | Strip reserved headers, stamp only the actual class | Fake spoof disappears; exact ingress stamps remain |
| All subscription bearers unavailable | Stable 403 + computed reset hint | Rejected windows return epoch/Retry-After, trap stays zero |
| Refusal enters AIMD/retry as capacity | 403 policy channel, never 503 | Refusal test asserts 403 + no AIMD touch |
| Health copies unbounded source state | One scalar summary per configured lane | Large-bearer fixture still yields bounded, fast health |
| Enforcement or stamp removed | Tests bind both controls | Mutation run must make each focused test fail |

## Project Structure

### Documentation (this feature)

```text
specs/094-subscription-eligibility/
├── checklists/requirements.md
├── contracts/credential-eligibility-v1.md
├── data-model.md
├── plan.md
├── quickstart.md
├── research.md
├── spec.md
├── tasks.md
└── verify.sh
```

### Source Code

```text
src/anthropic_throttle_proxy/
├── ingress.py
└── routing.py

tests/
├── test_ingress.py
├── test_ingress_adr6a.py
└── test_routing.py

docs/ARCHITECTURE.md
handoff.md
```

**Structure Decision**: Reuse the existing ingress/routing split. The
credential predicate and HTTP handling stay in `ingress.py`; `routing.py`
gains only the `LaneState` credential fields. No new runtime module or
dependency is warranted.

## Verification

1. Tests-first targeted gate:
   `uv run pytest -q tests/test_ingress_adr6a.py`.
2. Ruff lint and format check over `src tests`.
3. CI-equivalent `uv run pytest -q` (the suite mutates process-global state, so
   cross-file process fan-out is not a trustworthy gate).
4. `uvx mypy@2.1.0 --ignore-missing-imports --follow-imports=skip` on the two
   changed source modules; the repo-wide mypy baseline is recorded report-only.
5. `uv build`.
6. Docker build with the reviewed Git SHA build arg; no push.
7. Health size/latency oracle and eligibility near-miss fixtures.
8. Three temporary mutation runs: bypass required-mode selection, suppress the
   response stamp, then suppress spoof stripping; each focused test must fail
   before the committed files are restored.
9. Different-family adversarial review of the final diff, tests, live evidence,
   and no-deploy plan (generator family is OpenAI — reviewer must be Anthropic).

## Complexity Tracking

No constitution violation or new abstraction beyond the scalar lane-state
fields. The nested credential-mode tuple keeps the class + reason from being
lost whenever existing code replaces a `LaneState` during saturation handling.

## Reversal

`git revert <094-squash-sha>`
