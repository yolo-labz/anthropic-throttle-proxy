# Environment Variables Contract

Every environment variable the proxy reads, with default and effect.
Sourced from `src/anthropic_throttle_proxy/config.py`,
`src/anthropic_throttle_proxy/proxy.py`, and
`src/anthropic_throttle_proxy/ui/advisor_impl.py`. Authoritative; any
addition to source must be reflected here.

Most numeric knobs are also runtime-mutable via `POST /ui/config` —
the dashboard's config tab lists them. Topology knobs (`THROTTLE_UPSTREAM`,
`THROTTLE_CENTRAL_URL`, `THROTTLE_HOST`, `THROTTLE_PORT`,
`THROTTLE_QUEUE_MODE`) are env-only because changing them mid-flight
would corrupt in-flight requests; the dashboard shows them as
`restart required`.

## Wiring / topology (env-only, restart required to change)

| Variable | Default | Effect |
| --- | --- | --- |
| `THROTTLE_UPSTREAM` | `https://api.anthropic.com` | Sole upstream redirect target. Constitution Principle V — only redirect mechanism. |
| `THROTTLE_HOST` | `127.0.0.1` | Bind address. Loopback for local tier; `0.0.0.0` for Dokku container (handled by Dokku-injected `PORT`). |
| `THROTTLE_PORT` | `8765` | Bind port. |
| `THROTTLE_QUEUE_MODE` | `off` | `off` / `observe` / `fair` / `reactive` (alias of `fair`). |
| `THROTTLE_CENTRAL_URL` | `""` (disabled) | If set, requests forward to this central proxy first. |
| `THROTTLE_CENTRAL_HEALTH_INTERVAL` | `30` | Seconds between `/__throttle/health` polls against central. |
| `THROTTLE_CENTRAL_HEALTH_TIMEOUT` | `5` | Seconds before a central health poll is considered failed. |
| `THROTTLE_CENTRAL_FORWARD_TIMEOUT` | `10` | Connect timeout when forwarding a request to central. |

## Concurrency and pacing (runtime-mutable)

| Variable | Default | Effect |
| --- | --- | --- |
| `CLAUDE_API_THROTTLE_MAX` | `32` | Hard ceiling on in-flight requests per bearer. AIMD shrinks live cap below this on pushback. |
| `THROTTLE_MIN_DISPATCH_GAP_MS` | `0` | Minimum gap (ms) between two consecutive upstream POSTs. 0 disables burst pacing. |
| `THROTTLE_CENTRAL_LOCAL_MAX_CONCURRENT` | `2` | When `THROTTLE_QUEUE_MODE=off` AND `THROTTLE_CENTRAL_URL` is set, local admission cap to hold same-host bursts before central feedback returns. |

## AIMD reactive throttle (runtime-mutable)

| Variable | Default | Effect |
| --- | --- | --- |
| `THROTTLE_AIMD_MIN` | `1` | Floor of multiplicative-decrease. Live cap never drops below this. Constitution Principle III — must be ≥ 1. |
| `THROTTLE_AIMD_BACKOFF_S` | `30` | Seconds after a shrink before additive-increase can resume. |
| `THROTTLE_AIMD_RAMP_AFTER` | `10` | Consecutive 200s past the cooldown before live cap grows by +1. |
| `THROTTLE_AIMD_DECREASE` | `0.7` | Shrink factor on `429`/`503`. `0.5` = TCP Reno (deep teeth); `0.7` = CUBIC-style (default, higher avg utilization). |
| `THROTTLE_UTILIZATION_TARGET` | `0` | When > 0, proactively shrink the AIMD ceiling once the binding 5h/7d unified-window utilization crosses this fraction. `0` = disabled. |
| `THROTTLE_RATE_PUSHBACK_RETRIES` | `1` | Buffered retry count for upstream `429`/`503`/`529` responses. The proxy waits the upstream `Retry-After` value when present, otherwise `THROTTLE_AIMD_BACKOFF_S`, before retrying. |

## Body shrink (runtime-mutable, client-side guard)

These knobs are part of the proxy package but are an artifact of a
dropped feature (see memory file `request-crop-dropped`). They remain
configurable for environments that want a soft trim before forwarding,
but the default (`0`) disables the whole feature.

| Variable | Default | Effect |
| --- | --- | --- |
| `THROTTLE_BODY_SHRINK_CAP_BYTES` | `0` (disabled) | Soft body cap below which body_shrink does not trim. |
| `THROTTLE_BODY_SHRINK_KEEP_TURNS` | `4` | Trailing messages left untouched by the trimmer. |
| `THROTTLE_BODY_SHRINK_MIN_BLOCK_BYTES` | `2048` | Skip trimming `tool_result` blocks below this size. |

## Advisor (GROQ)

| Variable | Default | Effect |
| --- | --- | --- |
| `ADVISOR_ENABLED` | `false` | Master switch. When `true` AND `GROQ_API_KEY` is present, the advisor module is lazy-imported and the proxy fires diagnoses on `429`/`503`/`529`. |
| `GROQ_API_KEY` | unset | GROQ API key. Convention: `api/groq` in Bitwarden, fetched via `rbw get api/groq` for deploys. Never logged. |
| `ADVISOR_MODEL` | `llama-3.1-8b-instant` | GROQ model id. Switching to another OpenAI-compatible provider is configuration, not code change. |
| `ADVISOR_DEBOUNCE_S` | `120` | Minimum seconds between auto-triggered advisor calls. Prevents the advisor itself from being a throttle event. |

## Operator hygiene

- Secrets live in Bitwarden under `api/<service>`. Fetch via
  `rbw get api/groq`. Never paste keys into shells with history.
- `THROTTLE_UPSTREAM` is the only redirect mechanism. There is no
  per-route override and no in-code default beyond `api.anthropic.com`.
- All numeric knobs are bounded in `config.EDITABLE_KNOBS`; the UI and
  config-file loaders refuse out-of-range values.
- Changing `THROTTLE_QUEUE_MODE` or `THROTTLE_CENTRAL_URL` requires a
  restart; the UI labels them `restart required`.
