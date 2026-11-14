# Security Policy

Throttler (`throttler-gateway` / `throttler-ingress`) is a self-hosted HTTP gateway that sits in front of upstream model APIs and holds the credentials for them. It is not a multi-tenant SaaS: a compromise of the proxy is a compromise of every credential it can reach, on every machine that points at it. Read this before exposing a deployment beyond loopback.

## Supported versions

Security fixes land on `main` and on the newest published image/tag only. There are no backports to older pins — upgrade the deployment before reporting.

| Version | Supported |
|---|---|
| `main` / newest tag | ✅ |
| < current tag | ❌ — upgrade first |

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository (Security → Report a vulnerability). Do not open a public issue, and do not include live tokens, upstream API keys, `Retry-After`/utilization headers captured from a real account, or session ledger contents in a report — describe them, redact values.

Expected handling: acknowledgement within 7 days, triage against the threat table below, fix on `main` with a PR that names the advisory. There is no paid bounty program.

## Threat model

| # | Threat | Impact | Mitigation |
|---|---|---|---|
| T1 | Upstream credential exfiltration | A leaked Anthropic/OpenAI/z.ai key burns the account's quota and, for subscription OAuth tokens, gets the account flagged | Credentials are read from the environment or a secret file — never committed, never logged, never echoed by `/__throttle/health`, `/metrics` or the dashboard. Bearer values are redacted in error paths. Bind to loopback by default. |
| T2 | Unauthenticated ingress | Anything that can reach the ingress port can spend the owner's quota | The ingress/gateway are documented as loopback-or-private-network deployments behind an authenticating reverse proxy. Do not expose the dashboard or ingress port to the public internet; if you must, terminate TLS and require auth at the edge. |
| T3 | Prompt/response body logging | Inbound bodies carry user prompts and proprietary context | The proxy forwards request bodies; it does not persist them. Ledger and metrics record counters, IDs, model names and timing — not message content. When filing a bug, paste headers and counters, not bodies. |
| T4 | Denial of the owner's own capacity | A runaway client or a mis-set AIMD ceiling starves every TUI in the fleet | Fair per-bearer round-robin, AIMD shrink on `429/503`, `529` excluded from shrink, uncapped `Retry-After` honouring, and per-request subscription-lane requirements that fail closed with 403 instead of spilling onto a pay-go lane. |
| T5 | Supply-chain compromise of an upstream dependency (aiohttp, jinja2, pyyaml, prometheus-client) | Arbitrary code in the process that holds the credentials | Dependencies are locked (`uv.lock`), updated by Dependabot, and scanned by OSV-Scanner and Sonar in CI; GitHub Actions are pinned by commit SHA. |
| T6 | Container escape / privileged runtime | Host-level compromise from the published image | The image runs as a non-root user with no docker socket, no host mounts and no `--privileged`; deployment docs assume that. |

## Static analysis lanes

| Lane | Tool | What it catches |
|---|---|---|
| Code | CodeQL | Taint flows, injection into aiohttp handlers, unsafe deserialization |
| Dependencies | OSV-Scanner | Known CVEs in the locked dependency set |
| Types | mypy | Contract drift in the wire/ledger paths |
| Quality | SonarQube | Complexity and duplication hotspots in the admission logic |
| Workflows | Quality-gates (actionlint + zizmor) | Unpinned actions, over-broad `permissions:` |
| Supply chain | OpenSSF Scorecard | Pinned dependencies, token permissions, release provenance |

## Scope notes

- Out of scope: bugs that require an already-compromised host, already-stolen upstream credentials, or a deliberately hostile reverse proxy in front of the ingress.
- Configuration mistakes (public bind without auth, `THROTTLE_*` ceilings set to zero) are documentation issues first — report them as such unless they cause a credential or capacity failure on their own.
