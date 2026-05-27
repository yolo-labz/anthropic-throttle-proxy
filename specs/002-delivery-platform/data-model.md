# Data Model: Throttle Proxy Delivery Platform

In-process entities only. No database, no on-disk persistence beyond
logs. Every entity below lives in Python objects in the running aiohttp
process; all entries reset to defaults on process restart.

## Bearer

Logical identity derived from a client's `Authorization` header.
Externally identified by an 8-character SHA-256 prefix (`bearer_id`).
The raw header is never persisted.

| Field | Type | Source | Notes |
| --- | --- | --- | --- |
| `bearer_id` | `str` (8 hex chars) | `_bearer_id(request)` in `proxy.py` | `sha256(Authorization)[:8]`. Used in logs, metric labels, dashboard rows, health JSON. |
| `live_cap` | `int` | `FairBearerLimiter` | Current AIMD ceiling. Starts at `CLAUDE_API_THROTTLE_MAX`. Floor `THROTTLE_AIMD_MIN`. |
| `inflight` | `int` | `FairBearerLimiter` | Number of requests currently dispatched but not yet returned. |
| `queued_per_client` | `dict[client_id, int]` | `FairBearerLimiter` | Per-client backlog. Round-robin source. |
| `consecutive_pushback` | `int` | `FairBearerLimiter` | Increments on 429/503, resets on 200. Triggers shrink past threshold. |
| `consecutive_success` | `int` | `FairBearerLimiter` | Increments on 200, resets on pushback. Drives ramp after `THROTTLE_AIMD_RAMP_AFTER`. |
| `last_shrink_at` | `float` (epoch s) | `FairBearerLimiter` | For cooldown gate (`THROTTLE_AIMD_BACKOFF_S`). |
| `retry_after_deadline` | `float \| None` (epoch s) | `pacing.py::note_retry_after` | Earliest moment the next dispatch is allowed. Uncapped. |
| `last_ratelimit` | `dict[str, str]` | `_extract_ratelimit` | Snapshot of latest `anthropic-ratelimit-*` headers. |
| `unified` | `dict` | `_parse_unified` / `_apply_unified` | 5h/7d utilization + status + reset epoch. |
| `last_status` | `int \| None` | `forwarding.py` | Most recent upstream HTTP status. |

**Invariants**:
- `live_cap >= max(1, THROTTLE_AIMD_MIN)` always.
- `inflight <= live_cap` always (the queue holds the excess).
- Mutations require holding `bearer_limiter_lock` (constitution
  Don't break rule, also Principle III).

**Lifecycle**: lazy-allocated on first request for a new
`Authorization` header; survives until process restart. No TTL or
eviction in v1 (bearer count is bounded by the IDE fleet).

## Client

A peer of a bearer. Identified by peer host:port or, if present,
`X-Throttle-Client-Id` header. Used by the per-bearer fair queue to
round-robin across siblings sharing one bearer.

| Field | Type | Source | Notes |
| --- | --- | --- | --- |
| `client_id` | `str` | `request.transport.get_extra_info` or header | Stable for the duration of a TCP connection. |
| `queue_depth` | `int` | Derived from `bearer.queued_per_client[client_id]` | Live. |

**Lifecycle**: identifier survives only as long as the bearer holds a
backlog entry for it. The fair queue removes empty per-client entries
to avoid unbounded dict growth from short-lived clients.

## Central instance

A single external proxy at `THROTTLE_CENTRAL_URL` that enforces the
fleet-wide concurrency cap.

| Field | Type | Source | Notes |
| --- | --- | --- | --- |
| `url` | `str \| None` | `config.THROTTLE_CENTRAL_URL` | When `None`, central tier is disabled; proxy forwards directly to `THROTTLE_UPSTREAM`. |
| `status` | `"up" \| "down"` | `proxy.central_health_loop` | Polled every `THROTTLE_CENTRAL_HEALTH_INTERVAL` (default 30 s). |
| `last_poll_at` | `float` (epoch s) | health loop | For staleness in `/__throttle/health`. |
| `last_health_payload` | `dict` | health loop | Most recent `GET <central>/__throttle/health` body. |

**Invariants**:
- When `status == "down"`, the hot path MUST forward directly to
  `THROTTLE_UPSTREAM`. No request is held waiting for central recovery.
- `status` transitions are monotonic per poll: a single poll determines
  status; flapping is observable via consecutive flips in the journal,
  not via in-process hysteresis. (Hysteresis can be added later if
  flapping proves problematic in production; not in v1.)

## Advisor verdict

A GROQ-generated diagnosis of recent throttle events.

| Field | Type | Source | Notes |
| --- | --- | --- | --- |
| `verdict_text` | `str` | GROQ response | One- to three-sentence diagnosis. |
| `triggered_by` | `"auto" \| "manual"` | `proxy._maybe_advise` or `POST /ui/advisor` | `auto` = debounced from 429/503/529; `manual` = operator-initiated. |
| `triggered_at` | `float` (epoch s) | event time | For dashboard freshness. |
| `event_context` | `dict` | proxy state at trigger | Last 429/503/529 counts, current bearers in pushback, last `Retry-After`. Never includes raw bearer tokens or API keys. |
| `model` | `str` | config | GROQ model used (e.g. `llama-3.1-70b-versatile`). |

**Storage**: `state["last_advisor"]` (one slot, overwritten on each
new verdict). Rendered at `/ui` and exposed for programmatic access via
the same key.

**Invariants**:
- `event_context` MUST NOT contain raw `Authorization` headers, raw
  API keys, or raw OAuth tokens. Only `bearer_id` hashes.
- The advisor module is lazy-imported. Importing it before the first
  trigger is a regression.

## Inter-entity relationships

- One Bearer → many Clients (fair queue).
- Zero or one Central instance (singleton; `None` when disabled).
- Zero or one current Advisor verdict (singleton slot).
- Bearer state survives a central status flip — central down does not
  reset AIMD state.
- Advisor verdicts reference bearers by `bearer_id` only.

## State that is intentionally NOT persisted

- Bearer state on restart (intentional: a restart implies operator
  intent to reset the throttle).
- Advisor verdicts on restart (intentional: stale verdicts are worse
  than no verdict).
- Central status on restart (intentional: re-polled within one health
  interval).

Adding any of these to disk would be a v2 decision and would require
re-evaluating constitution Principle II (no secret material on disk).
