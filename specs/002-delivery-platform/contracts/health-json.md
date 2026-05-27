# `/__throttle/health` JSON Schema

Authoritative shape of the health endpoint, sourced from
`proxy.py::health`. Polled by the Dokku healthcheck every 5 s with a
5 s timeout and by each local proxy's central health loop on its own
`THROTTLE_CENTRAL_HEALTH_INTERVAL`. p99 latency must stay < 50 ms
(constitution Principle IV).

## Top-level fields

| Field | Type | Source | Notes |
| --- | --- | --- | --- |
| `inflight` | int | `state["inflight"]` | Total in-flight requests across all bearers. |
| `queued` | int | `state["queued"]` | Total queued requests across all bearers. |
| `served` | int | `state["served"]` | Counter of completed requests since process start. |
| `client_disconnects` | int | `state["client_disconnects"]` | Counter of clients that disconnected mid-stream. |
| `upstream_retries` | int | `state["upstream_retries"]` | Counter of internal upstream retries. |
| `max_concurrent` | int | `config.MAX_CONCURRENT` | Configured hard ceiling per bearer (env / runtime override). |
| `queue_mode` | string | `config.QUEUE_MODE` | `off` / `observe` / `fair` (or `reactive`). |
| `min_dispatch_gap_ms` | int | `config.MIN_DISPATCH_GAP_S * 1000` | Burst pacing gap. |
| `upstream` | string | `config.UPSTREAM` | Configured upstream URL (constitution Principle V). |
| `central_url` | string | `config.CENTRAL_URL` | Empty string when central tier is disabled. |
| `central_status` | string | `state["central_status"]` | `up` / `down` / `unknown`. |
| `central_last_check` | float | `state["central_last_check"]` | Epoch seconds of the last central health poll. `0` until first poll. |
| `last_advisor` | object \| null | `state["last_advisor"]` | Latest advisor verdict. See **Advisor verdict** below. |
| `bearers` | object | `bearer_state` | Per-bearer view. See **Bearers** below. |

## `bearers` (object, keyed by `bearer_id`)

Each value carries that bearer's view of the system. Per-bearer state
is the union of `bearer_state[bid]` and `bearer_limiters[bid].snapshot()`.

| Field | Type | Notes |
| --- | --- | --- |
| `inflight` | int | Per-bearer in-flight count. |
| `queued` | int | Per-bearer queue depth. |
| `served` | int | Per-bearer served counter. |
| `clients` | int \| object | Set of clients seen for this bearer. |
| `limiter` | object | `FairBearerLimiter.snapshot()` — live cap + queue depths. |

### `bearers[bid].limiter` (object)

| Field | Type | Notes |
| --- | --- | --- |
| `live_cap` | int | Current AIMD ceiling (≥ `THROTTLE_AIMD_MIN`). |
| `hard_max` | int | Configured ceiling (`MAX_CONCURRENT` or `CENTRAL_LOCAL_MAX_CONCURRENT` depending on topology). |
| `inflight` | int | Same as `bearers[bid].inflight`; carried here for limiter-internal consistency. |
| `queued_per_client` | object | `client_id → backlog count`. Round-robin source for the fair queue. |
| `consecutive_pushback` | int | Run length of 429/503 since last 200. |
| `consecutive_success` | int | Run length of 200 since last pushback. |
| `last_shrink_at` | float \| null | Epoch seconds of the last AIMD shrink. `null` until first shrink. |
| `retry_after_deadline` | float \| null | Epoch seconds before which the next dispatch is gated. |
| `unified` | object \| null | OAuth unified-window state. See below. |

### `bearers[bid].limiter.unified` (object, OAuth bearers only)

| Field | Type | Notes |
| --- | --- | --- |
| `utilization_5h` | float | 0..1; fraction of 5h budget consumed. |
| `status_5h` | string | `allowed` / `allowed_warning` / `rejected`. |
| `reset_5h` | float | Epoch seconds when the 5h window resets. |
| `utilization_7d` | float | 0..1; fraction of 7d budget consumed. |
| `status_7d` | string | Same domain as `status_5h`. |
| `reset_7d` | float | Epoch seconds when the 7d window resets. |

For API-key bearers (non-OAuth), `unified` is `null`.

## `last_advisor` (object \| null)

| Field | Type | Notes |
| --- | --- | --- |
| `text` | string | Verdict text from GROQ. One- to three-sentence diagnosis. |
| `ts` | float | Epoch seconds of the trigger event. |
| `trigger` | string | `auto` (debounced from 429/503/529) or `manual` (from `POST /ui/advisor`). |

`null` when the advisor has not run yet in this process.

## Invariants enforced by tests

- `central_status` ∈ {`up`, `down`, `unknown`}.
- `queue_mode` ∈ {`off`, `observe`, `fair`, `reactive`}.
- No raw `Authorization` header, raw API key, or raw OAuth token appears
  anywhere in the response (constitution Principle II). Bearer IDs are
  always 8-character SHA-256 prefixes.
- `live_cap >= max(1, THROTTLE_AIMD_MIN)` for every bearer.

## Example (Pedro's desktop, after recent activity)

```json
{
  "inflight": 0,
  "queued": 0,
  "served": 412,
  "client_disconnects": 1,
  "upstream_retries": 0,
  "max_concurrent": 8,
  "queue_mode": "off",
  "min_dispatch_gap_ms": 50,
  "upstream": "https://api.anthropic.com",
  "central_url": "https://anthropic-throttle.home301server.com.br",
  "central_status": "up",
  "central_last_check": 1748275442.31,
  "last_advisor": {
    "text": "5h unified window at 78%; 7d at 24%. Shrink to 4 slots until 19:00 reset to keep the 5h headroom.",
    "ts": 1748275398.11,
    "trigger": "auto"
  },
  "bearers": {
    "abc12345": {
      "inflight": 0,
      "queued": 0,
      "served": 412,
      "clients": 3,
      "limiter": {
        "live_cap": 6,
        "hard_max": 8,
        "inflight": 0,
        "queued_per_client": {},
        "consecutive_pushback": 0,
        "consecutive_success": 18,
        "last_shrink_at": 1748275212.05,
        "retry_after_deadline": null,
        "unified": {
          "utilization_5h": 0.78,
          "status_5h": "allowed_warning",
          "reset_5h": 1748293200.0,
          "utilization_7d": 0.24,
          "status_7d": "allowed",
          "reset_7d": 1748794800.0
        }
      }
    }
  }
}
```
