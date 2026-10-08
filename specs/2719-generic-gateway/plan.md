# Implementation plan: Throttler

**Branch**: `2719-generic-gateway` | **Date**: 08/10/2026 | **Spec**: [spec.md](spec.md)

## Summary and hypothesis

The runtime already routes configurable upstreams and existing protocol adapters;
provider branding remains in distribution, entry points and presentation. Thin
canonical entry points can migrate product identity without duplicating mutable
runtime state. A separate runtime or changed wire/state behavior falsifies this.

## Technical context

Python >=3.13; existing aiohttp/Jinja/Prometheus/PyYAML, uv and hatchling.
No new dependencies. Package `throttler_gateway` delegates to the retained
implementation package. Storage, settings, headers, metric names, root probe
bodies and provider metadata stay compatible. Update the root lockfile identity
without dependency churn. Docker/Procfile use the canonical gateway command.

## Constitution check (before and after design)

I: no vendor SDK; II: no secrets; III: limiter unchanged; IV: local probes
unchanged; V: existing routing knobs and HTMX remain authoritative. Source
worktree is isolated. Source rollback is one revert PR. No live acceptance
claim or cross-service deployment is part of this PR.

## Structure and sequence

1. Inspect package, generic routing/config, callers and assets (complete).
2. Record contracts/research, create tasks and bounded regression acceptance.
3. Add canonical distribution/package/commands and preserve legacy aliases.
4. Update dashboard/brand/current documentation and operational samples.
5. Admitted focused/full tests, wheel/sdist acceptance, lint, normal hooks.
6. PR, exact-head green required CI, resolved threads, safe squash merge.

The checked-in Speckit commands/templates guide this sequence. `setup-plan.sh`
rejects the operator-assigned four-digit branch (`2719-generic-gateway`) because
its validator accepts three digits or timestamps. Artifacts are populated here
directly; the branch and workflow scripts are not renamed or loosened.

Agent context: this plan introduces no technology; current `CLAUDE.md` product
overview/quickstart will be updated directly without generated context churn.
