# Architecture

`anthropic-throttle-proxy` is one aiohttp binary with two operating modes:

- **local**: a host-local `ANTHROPIC_BASE_URL` target for Claude Code,
  opencode, codex, and SDK clients. It can forward to a central tier and fall
  back direct to Anthropic when central health is bad.
- **central**: the fleet-wide admission point. It owns the fair queue,
  per-bearer AIMD ceiling, upstream retry handling, and telemetry surfaces.

The same process can serve `/__throttle/health`, `/metrics`, `/ui`, and the
proxy path. The hot path never depends on the optional GROQ advisor or HTMX UI.

```text
Claude clients
  |
  | ANTHROPIC_BASE_URL=http://127.0.0.1:8765
  v
local proxy
  |  THROTTLE_CENTRAL_URL set
  v
central proxy
  |
  v
api.anthropic.com
```

## Request Path

1. The client sends an Anthropic-compatible request to the proxy.
2. The proxy derives a bearer id from the `Authorization` header without
   storing or logging the token.
3. Local mode optionally forwards to the configured central URL. If central is
   unhealthy, local mode can fall back to direct upstream dispatch.
4. Central mode admits the request through the configured queue mode:
   `off`, `observe`, `fair`, or `reactive`.
5. The upstream response updates per-bearer limiter state from status codes,
   `Retry-After`, and `anthropic-ratelimit-*` headers.

The proxy deliberately returns local fast-fail responses for known long-window
blocks instead of letting every tab rediscover the same upstream rejection.

## Subscription-constrained ingress requests (ADR-6a)

The unified `:8760` ingress supports an opt-in pre-egress contract for
callers that must not spend direct/pay-go capacity:

```http
X-Anthropic-Throttle-Require-Credential-Mode: subscription
```

A compliant caller first checks `GET /__throttle/health` for the
`enforcement` capability object (`credential_mode:true`, `contract="adr6a-credential-mode/1"`,
`subscription_upstreams_count` + `subscription_upstreams_digest`). That
discovery step is mandatory: an old ingress cannot know that an unknown
header should fail closed, so absence of the capability means **do not send the
model request**.

The ingress classifies each lane from trusted health facts, never its name,
`detail` string, or credential custody. Eligibility is the frozen conjunction:

- CLASS (E1∧E2∧E4): `api_key.enabled=false`; canonical upstream ∈
  `INGRESS_SUBSCRIPTION_UPSTREAMS` (https, allowlisted host, port 443/absent,
  empty path, no userinfo/query/fragment; empty allowlist ⇒ no lane eligible);
  lane URL loopback ∧ `central_url==""`.
- CAPACITY (E3): ≥1 usable bearer carrying fresh 5h/7d window state.

The predicate is applied to session pins, initial selection, spill, and retry
without replacing shared unconstrained pins. For a constrained request the
candidate lane's health is re-probed at selection time so the decision is
attributable to the request served. No eligible capacity returns a pre-egress
`403` policy verdict (`x-anthropic-throttle-refusal: no_eligible_lane |
eligible_lanes_exhausted`), never a capacity 503; reset hints appear only for a
genuine all-bearer budget closure. Widened constrained failures stay
request-local and cannot poison shared lane availability.

Constrained responses only (r1/C3): a request carrying the requirement header
gets `X-Anthropic-Throttle-Credential-Mode: subscription` on a served 2xx and
`unknown` on the 403 refusal — unconstrained traffic is behaviorally unchanged.
The full four-value vocabulary lives in per-lane health. Same-named upstream
headers are always stripped (anti-spoof). Full wire
contract and falsifiers:
[`specs/094-subscription-eligibility/contracts/credential-eligibility-v1.md`](../specs/094-subscription-eligibility/contracts/credential-eligibility-v1.md).

## Limiting Model

Limiter state is keyed by bearer id. Each bearer gets:

- a configured hard ceiling from `CLAUDE_API_THROTTLE_MAX`;
- a live AIMD ceiling bounded by `THROTTLE_AIMD_MIN`;
- optional burst smoothing from `THROTTLE_MIN_DISPATCH_GAP_MS`;
- upstream retry windows from `Retry-After`;
- OAuth window utilization from Anthropic's unified ratelimit headers.

`429` and `503` are treated as rate pushback. `529` is tracked separately as
upstream overload and does not shrink the bearer ceiling.

When an OAuth 5h or 7d window is already rejected, the proxy pauses that bearer
until the reported reset. This avoids a storm of identical upstream failures.

## Credential Failover

Multi-account desktop hosts use one Claude Code credentials file per account.
The proxy has three opt-in credential features:

- `THROTTLE_ACCOUNT_CRED_PATHS`: maps account labels to credentials files so
  `/ui` can show which live bearer belongs to which local account.
- `THROTTLE_ACCOUNT_ROUTING=least_loaded`: local-only hot-path routing. For
  each `POST /v1/messages`, the proxy chooses the least-loaded usable account
  from `THROTTLE_ACCOUNT_CRED_PATHS` and rewrites only the upstream
  `Authorization` header. This is how already-running Claude sessions share
  multiple accounts without restarting or re-reading credentials.
- `THROTTLE_ACTIVE_CRED_PATH`: names the single active credentials file used by
  the fleet. When a stale tab is still sending an old bearer while the active
  file has moved to a different bearer, the proxy returns a local 401 nudge:
  `throttle-proxy: active account changed; re-read credentials`.

That 401 is intentional. Claude Code self-heals by re-reading the active
credentials file. Do not recover this state with an interactive `/login` inside
each tab; clean exit/resume preserves the session and picks up the right file.

`THROTTLE_ACTIVE_CRED_PATH` nudges are disabled while account routing is enabled,
because the router already owns account choice per request. The proxy cannot
refresh OAuth tokens, repair dead refresh tokens, or prove two credential files
belong to different human accounts unless local account metadata is configured
and current.

## Observability

- `/__throttle/health` is the machine-readable incident endpoint. Use it before
  changing queue or credential behavior.
- `/metrics` exposes Prometheus counters and gauges, including credential nudge
  counts and account identity state.
- `/ui` renders the operational dashboard. If every configured credential file
  maps to the same profile email, the UI reports account collapse because
  account switching is cosmetic in that state.

## Deployment

Dokku runs the central tier from this repository. Local desktop integration is
owned by the NixOS configuration and points Claude clients at the local proxy.
Deploy central changes with the Dokku remote after CI is green and the PR is
merged. Desktop behavior is persisted through the NixOS/Home Manager service
configuration and must be verified with `systemctl --user show` plus
`/__throttle/health`.

```sh
git push dokku main
```

Desktop-only credential repair usually needs NixOS activation and tab
exit/resume, not a Dokku deploy. Record those incidents in `handoff.md` so the
next session can distinguish proxy bugs from host credential state.

## Quality Gates

Required PR CI runs `ruff check`, `ruff format --check`, and the full pytest
suite from the locked `uv.lock` dependency graph. The SonarQube workflow runs
the same locked dependency graph and publishes `coverage.xml` for
`sonar-project.properties`.

Throttle-path fixes need tests for the exact failure path and a live readback
after deployment. Documentation-only and CI-only changes do not need a
throttle-path adversarial review, but they still go through PR CI before merge.
