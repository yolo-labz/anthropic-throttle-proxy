# HTTP Routes Contract

The proxy exposes a small fixed surface. Every route below is local
(answered without forwarding upstream) EXCEPT the catch-all `*`, which
is the entire reason the proxy exists.

## Probe and observability surface (local, no bearer slot consumed)

### `GET /` and `HEAD /`

Local root probe. Added in PR #29 specifically because load balancers,
Dokku healthchecks, and `curl` smoke tests hit `/` and the catch-all
would otherwise forward those probes to `api.anthropic.com`, consuming
quota and returning a meaningless 404 from Anthropic.

| Aspect | Value |
| --- | --- |
| Method | `GET`, `HEAD` |
| Path | `/` (exact) |
| Auth | None |
| Bearer queue | NOT consumed |
| Response status | `200 OK` |
| Response body | Short human-readable string identifying the service |
| Forwarded upstream | NO |
| Governed by | Constitution Principle IV |

**Contract test**: `tests/test_proxy_app.py::test_root_probe_get` and
`test_root_probe_head` verify the response is 200 and that
`THROTTLE_UPSTREAM` is not contacted.

### `GET /__throttle/health`

Local health endpoint. Polled by the Dokku healthcheck every 5 s with a
5 s timeout, by the central health loop on every local proxy, and by
operator `curl` invocations. MUST return in < 50 ms.

| Aspect | Value |
| --- | --- |
| Method | `GET` |
| Path | `/__throttle/health` |
| Auth | None |
| Bearer queue | NOT consumed |
| Response status | `200 OK` always (the body carries health state) |
| Response body | JSON — schema in [health-json.md](./health-json.md) |
| Forwarded upstream | NO |
| Governed by | Constitution Principle IV |

### `GET /metrics`

Prometheus exposition. Process-local `CollectorRegistry`, not the
global default.

| Aspect | Value |
| --- | --- |
| Method | `GET` |
| Path | `/metrics` |
| Auth | None |
| Bearer queue | NOT consumed |
| Response status | `200 OK` |
| Response body | Prometheus text exposition format |
| Forwarded upstream | NO |
| Governed by | FR-014 |

**Metric families (non-exhaustive)**:
- `anthropic_requests_total{status, bearer_id}` — request counter.
- `anthropic_overload_total{bearer_id}` — `529` counter (carve-out).
- `anthropic_ratelimit_unified_5h_utilization{bearer_id}` — gauge.
- `anthropic_ratelimit_unified_7d_utilization{bearer_id}` — gauge.
- `anthropic_bearer_live_cap{bearer_id}` — current AIMD ceiling.
- `anthropic_bearer_inflight{bearer_id}` — current dispatch count.
- `anthropic_bearer_queued{bearer_id}` — current backlog.

## UI surface (local, server-rendered, HTMX swap targets)

### `GET /ui`

Renders the HTMX dashboard. Server-side Jinja2.

| Aspect | Value |
| --- | --- |
| Method | `GET` |
| Path | `/ui` (also `/ui/`) |
| Auth | None |
| Response status | `200 OK` |
| Response body | HTML (Jinja2 template `dashboard.html`) |
| JS deps | One `<script>` tag for HTMX 1.x. No Alpine, no React, no ESM. |
| Styling | Catppuccin Mocha tokens via `ui/static/style.css`. No raw hex outside the tokens file. |
| Governed by | FR-017, Constitution Principle II (no raw tokens in DOM) |

### `GET /ui/partials/stats`

HTMX swap target. Returns the stats fragment.

| Aspect | Value |
| --- | --- |
| Method | `GET` |
| Path | `/ui/partials/stats` |
| Response status | `200 OK` |
| Response body | HTML fragment (`partials/stats.html`) |

### `GET /ui/partials/config`

HTMX swap target. Returns the config fragment showing current
environment-variable-driven settings.

### `GET /ui/partials/advisor`

HTMX swap target. Returns the latest advisor verdict if any, otherwise
an empty-state placeholder.

### `POST /ui/advisor`

Triggers an on-demand advisor call (when `ADVISOR_ENABLED=true` and
`GROQ_API_KEY` is set). Result written to `state["last_advisor"]`.

| Aspect | Value |
| --- | --- |
| Method | `POST` |
| Path | `/ui/advisor` |
| Response status | `200 OK` (verdict returned as HTML fragment for HTMX swap) or `503` if disabled/unconfigured |
| Governed by | FR-018, FR-019 |

### `GET /ui/static/{file}`

Static assets — `favicon.svg`, `style.css`. Served via aiohttp's
`add_static`.

## Hot path (forwarded upstream, bearer slot consumed)

### `*` (catch-all)

Every other request — `POST /v1/messages`, `POST /v1/messages?beta=…`,
`POST /v1/complete`, and any future Anthropic endpoint — flows through
the catch-all handler.

| Aspect | Value |
| --- | --- |
| Method | Any |
| Path | Any not matched above |
| Auth | Pass through `Authorization` header unchanged |
| Bearer queue | Consumed (per-bearer fair queue + AIMD) |
| Response status | Pass through (200, 4xx, 5xx, 529) |
| Response body | Streamed pass-through |
| Forwarded to | `THROTTLE_CENTRAL_URL` if set and healthy; else `THROTTLE_UPSTREAM` |
| Governed by | FR-001, FR-002, FR-003, FR-010, Constitution Principle V |

**Headers preserved**:
- Request: `Authorization`, `anthropic-version`, `anthropic-beta`,
  `content-type`, `accept`, `accept-encoding`, `x-throttle-client-id`
  (optional, for client-id override).
- Response: every `anthropic-ratelimit-*`, `retry-after`, `content-type`,
  `transfer-encoding` (for SSE streams).

**Headers extracted** (and stored in `bearer_state[bid]`):
- `anthropic-ratelimit-*` family.
- `anthropic-ratelimit-unified-{5h,7d}-{utilization,status,reset}`.
- `retry-after`.

**Streaming**: SSE responses are streamed chunk-by-chunk. `usage`
blocks are parsed in-stream by `pricing.py` to emit token/cost metrics
without buffering the body.

## Methods NOT exposed

- `OPTIONS *` is handled by aiohttp's default; CORS is not configured
  in v1 (the proxy is loopback-only on the local tier and same-origin
  on the Dokku tier).
- `TRACE *`, `CONNECT *` are not handled.

## Error semantics

| Source | Status | Behavior |
| --- | --- | --- |
| Upstream returns 200 | 200 | Pass through. Increment success. |
| Upstream returns 4xx other than 429 | 4xx | Pass through. No throttle action. |
| Upstream returns 429 | 429 | Pass through. Shrink bearer cap. Honor `Retry-After`. Maybe trigger advisor. |
| Upstream returns 503 | 503 | Pass through. Shrink bearer cap. Maybe trigger advisor. |
| Upstream returns 529 | 529 | Pass through. Increment `anthropic_overload_total`. DO NOT shrink. Maybe trigger advisor. |
| Central unreachable | (transparent) | Fall back to direct `THROTTLE_UPSTREAM`. `central_status=down` in health JSON. |
| `Authorization` missing | (no special handling) | `_anon` bearer slot. Forwarded upstream; Anthropic returns 401. |
