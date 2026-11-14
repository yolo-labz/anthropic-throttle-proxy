# Spec 381 — Prompt-cache health panel (`/ui/cache`)

Coordinator row `ui-cache-health-panel`, 08/11/2026. It depends on #30 (spec 380), which supplies the shared `.usage-*` and `.u-<class>` styles.

## Sources (in-process; no new instrumentation)

| source | what it is |
|---|---|
| `metrics.M_TOKENS` (`anthropic_tokens_total{model,kind}`) | Kinds are `input`, `cache_read`, `cache_creation` and `output`. Fed by `ratelimit._parse_sse_usage` for the Anthropic and the OpenAI-compatible vocabularies. `input` is the **uncached remainder**. The counter increments only on a positive count, so a kind with no sample stays absent. |
| `session_affinity.stats()` | `new` / `kept` / `migrated` / `evicted` / `pins`. This is the same dict `/__throttle/health` embeds, but it is read here directly, not via health. |

The scope anchor is `account_pool.BALANCER.started_at`, the same "since service start" that the account table's *observed tokens · share* column uses. That table aggregates per account across all kinds, and this panel splits per model per kind, so the two don't duplicate each other. Both read from the same process.

## Semantics (OpenTelemetry GenAI)

- Input total = `input + cache_read + cache_creation`, matching `gen_ai.usage.input_tokens`. The cache kinds are subsets of it.
- Cache-read share = `cache_read / input total`. Cache-write share = `cache_creation / input total`. Output is shown separately and is never added to the input total.
- **Unknown, not zero.** A kind never observed for a model renders as `unknown`.
  - If `cache_creation` is missing, the input total renders as `≥ N` and the read share as `≤ x%`, an upper bound.
  - If `input` or `cache_read` is missing, the share is `unknown`.
- With no counters at all, the panel shows **no traffic yet**, never `0 %`.
- No USD. `M_COST` is a list-price estimate, not a debit, so it is not read.
- Cache writes are **attributed overhead**, never "waste". Cache scope is per workspace, so any account, provider or model switch is a guaranteed cold miss.
- The label reads `since gateway start <DD/MM/YYYY HH:MM> BRT, this process only`.

## Contract with the rest of the app

- `/__throttle/health` is untouched; a test asserts it never builds the panel.
- `GET /ui/cache` is polled every 10 s, using HTMX 1.x, a server-rendered SVG input-mix bar and Catppuccin tokens only.
- Auth: `/ui/cache` is mounted through `ui/auth.guard` (restored by #39), so a remote peer without the token gets 401.

## Acceptance

`tests/test_cache_health_panel.py` covers:
- the fixture share math, based on the live read at 23:11 BRT;
- a missing kind showing as unknown;
- zero counters showing "no traffic yet";
- no `$` and no "waste" in the HTML;
- a real-counter round trip;
- health independence.
