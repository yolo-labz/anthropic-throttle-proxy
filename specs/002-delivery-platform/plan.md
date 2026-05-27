# Implementation Plan: Throttle Proxy Delivery Platform

**Branch**: `002-delivery-platform` | **Date**: 2026-05-26 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification at `specs/002-delivery-platform/spec.md`

## Summary

Ship the `anthropic-throttle-proxy` as a coherent, fleet-deployable platform.
The codebase already implements the hot path, AIMD pacing, header/unified-window
parsing, central fallback, HTMX dashboard, and the GROQ advisor. This plan
captures the remaining delivery gaps — verifying that what ships matches
what runs (persistence), that documentation and skills agree with the
codebase invariants, and that the existing test suite covers the eight
success criteria from the spec.

The approach is mostly **verification + documentation harmonization** rather
than new feature work. The code is in place. The gap is between
constitution + spec + plan + tasks + docs + skills + live host.

## Technical Context

**Language/Version**: Python 3.13+
**Primary Dependencies**: `aiohttp` (server + raw `ClientSession` for the
advisor's GROQ call), `aiohttp-jinja2` (templates), `prometheus-client`
(metrics with a process-local `CollectorRegistry`). NO vendor AI SDK
(`anthropic`, `openai`, `groq`) anywhere on the hot path — constitution
Principle I.
**Storage**: In-process state only (`bearer_limiters`, `bearer_state`,
`state["last_advisor"]`). No database. No on-disk persistence beyond logs.
**Testing**: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). Existing
suites: `test_advisor.py`, `test_body_shrink.py`, `test_config_overrides.py`,
`test_forwarding_paths.py`, `test_pacing.py`, `test_proxy_app.py`,
`test_unified.py`. 89 tests pass in 2.5 s today.
**Target Platform**:
  - Local tier: Linux x86_64 desktop running NixOS / Home Manager user
    service. Pedro's host today.
  - Central tier: Linux container on Dokku (`anthropic-throttle.<host>`),
    Dockerfile-based multi-stage `uv` build per Astral's official pattern.
**Project Type**: Web service (HTTP reverse proxy, single Python process).
**Performance Goals**:
  - `/__throttle/health` p99 < 50 ms under sustained load equal to the
    configured per-bearer ceiling (Dokku's 5 s healthcheck timeout requires
    well-under-50-ms latency to stay green).
  - Hot path: streaming forwarder, no body buffering beyond `aiohttp`'s
    default chunked-encoding window.
  - Central health loop: poll every `THROTTLE_CENTRAL_HEALTH_INTERVAL`
    (default 30 s); fallback decision within one interval.
**Constraints**:
  - Single-worker (one Python process per tier). No multi-worker scaling
    required for v1.
  - `THROTTLE_AIMD_MIN ≥ 1` always — the floor is the safety net
    (constitution Principle III). The cap can never reach 0.
  - HTMX 1.x dashboard with one `<script>` tag, no ESM, no Alpine, no React.
    Catppuccin Mocha tokens only (no raw hex outside the tokens file).
  - Bearer tokens never appear in operator-visible surfaces. Only the
    8-character SHA-256 prefix `bearer_id` (constitution Principle II).
  - `THROTTLE_UPSTREAM` is the only upstream redirect mechanism
    (constitution Principle V).
**Scale/Scope**: Pedro's desktop + one Dokku app + ~5 LLM-IDE clients per
host (`claude-code`, `opencode`, `codex`, Anthropic SDK callers). Total
fleet today: ~3 hosts.

## Constitution Check

Gates derived from `.specify/memory/constitution.md` v1.0.0:

| Principle | Status | Evidence |
| --- | --- | --- |
| I. No Vendor AI SDK on the Hot Path | ✅ pass | `grep -RE 'import (anthropic\|openai\|groq)' src/anthropic_throttle_proxy/proxy.py src/anthropic_throttle_proxy/forwarding.py src/anthropic_throttle_proxy/limiter.py` returns no matches; advisor is in `ui/advisor_impl.py` and uses raw `aiohttp`. |
| II. Bearer Identity is a Hash, Never a Secret | ✅ pass | `_bearer_id` in `proxy.py` SHA-256-prefixes; FR-015 + acceptance scenario US3.3 cover the assertion; tests in `test_proxy_app.py` verify no raw token appears in metrics labels. |
| III. AIMD Floor is the Safety Net | ✅ pass | `limiter.FairBearerLimiter.shrink_on_pushback` clamps to `THROTTLE_AIMD_MIN` (default 1); `test_pacing.py` covers shrink/ramp/floor; `529` carve-out tested. |
| IV. Health Probes are Always Local and Cheap | ✅ pass | PR #29 added local `GET /` + `HEAD /`; `/__throttle/health` already local; `test_proxy_app.py::test_root_probe_*` verifies. Live: `curl /__throttle/health` < 50 ms. |
| V. Single Source of Truth for Upstream & Central Routing | ✅ pass | Only `THROTTLE_UPSTREAM` + `THROTTLE_CENTRAL_URL` are consulted in `forwarding.py`; no hard-coded URLs anywhere. |

All gates pass on first evaluation. No deviations to justify in the
Complexity Tracking table.

Re-evaluation after Phase 1 design: no new constraints introduced.

## Project Structure

### Documentation (this feature)

```text
specs/002-delivery-platform/
├── plan.md                # This file (/speckit.plan output)
├── spec.md                # /speckit.specify output (committed 8bddfff)
├── research.md            # Phase 0 output (this command)
├── data-model.md          # Phase 1 output (this command)
├── quickstart.md          # Phase 1 output (this command)
├── contracts/
│   ├── http-routes.md     # Public HTTP contract
│   ├── env-vars.md        # Configuration surface
│   └── health-json.md     # /__throttle/health JSON schema
├── checklists/
│   └── requirements.md    # /speckit.specify quality checklist
└── tasks.md               # Phase 2 output (/speckit.tasks command)
```

### Source Code (repository root)

```text
src/anthropic_throttle_proxy/
├── __init__.py
├── __main__.py            # python -m anthropic_throttle_proxy entrypoint
├── proxy.py               # main() + handler() (hot path) + root_probe + health
├── forwarding.py          # upstream / central forwarding + streaming
├── limiter.py             # FairBearerLimiter + AIMD shrink/ramp
├── pacing.py              # Retry-After + dispatch gap + unified window
├── ratelimit.py           # _extract_ratelimit + _parse_unified
├── config.py              # env-var parsing, defaults
├── metrics.py             # CollectorRegistry + counters + gauges
├── pricing.py             # SSE usage block parsing → token/cost metrics
├── body_shrink.py         # request-body trimming (client-side, see memory)
└── ui/
    ├── __init__.py
    ├── routes.py          # attach_ui — HTMX endpoints + dashboard render
    ├── advisor_impl.py    # GROQ call (raw aiohttp, lazy-imported)
    ├── static/
    │   ├── favicon.svg
    │   └── style.css      # Catppuccin Mocha tokens
    └── templates/
        ├── dashboard.html
        └── partials/
            ├── advisor.html
            ├── config.html
            └── stats.html

tests/
├── test_advisor.py
├── test_body_shrink.py
├── test_config_overrides.py
├── test_forwarding_paths.py
├── test_pacing.py
├── test_proxy_app.py
└── test_unified.py
```

**Structure Decision**: Single project — `src/anthropic_throttle_proxy/`
package with `ui/` submodule, sibling `tests/`. The package is the only
build artifact; no library/CLI split. The Dockerfile invokes
`python -m anthropic_throttle_proxy`. This matches the existing layout
1:1; no relocation is part of this delivery.

## Complexity Tracking

> Constitution Check has no violations to justify. This section is empty.

---

## Phase outputs (generated by this command)

- `research.md` — Decisions and rationale for non-obvious choices already
  baked into the codebase, captured so the plan is auditable.
- `data-model.md` — `Bearer`, `Client`, `Central instance`, `Advisor verdict`
  with concrete attribute lists.
- `contracts/http-routes.md` — Every HTTP route the proxy exposes, with
  expected method, path, response shape, and constitution principle that
  governs it.
- `contracts/env-vars.md` — Every `THROTTLE_*` / `ADVISOR_*` / `GROQ_*`
  variable the proxy reads, with default and effect.
- `contracts/health-json.md` — Concrete schema of the
  `/__throttle/health` JSON response.
- `quickstart.md` — Smallest possible "I just deployed this, prove it
  works" recipe.
