# 304 result — OpenAI prompt/cache conservation

Base `b5a08ee0` → head (this branch). Owner: pH. Frozen contract:
`specs/304-openai-conservation/contract.md`.

## What changed (allowed paths only)

1. `src/anthropic_throttle_proxy/ratelimit.py` — `_parse_sse_usage` OpenAI
   branch: `prompt_tokens` is inclusive of `prompt_tokens_details.cached_tokens`,
   so normalized kinds are now DISJOINT and CONSERVE it —
   `input + cache_read == prompt_tokens`, clamped on inconsistent payloads
   (`cached > prompt`) so nothing is double counted or silently dropped.
   Anthropic branch untouched (already disjoint).
2. `src/anthropic_throttle_proxy/ui/templates/partials/stats.html` — gauge
   LABEL only: the readout counts input + cache reads + cache writes (TOTAL
   prompt-side), so `input X/s` → `in+cache X/s (prompt-side total, cache
   included)`. The number's meaning is unchanged on purpose (contract: never
   swap total-vs-fresh to shrink a number).
3. `tests/test_ratelimit_usage.py` (new) + `tests/test_tps_gauge.py` —
   conservation, clamp, Anthropic regression, mixed-block conservation, the
   `parser -> record_usage sum -> history -> gauge` chain, and a label pin.

## Verify gate (executed at this head)

- RED first: `uv run pytest tests/test_ratelimit_usage.py tests/test_tps_gauge.py`
  → **7 failed, 9 passed** (conservation/chain/label all red-capable).
- GREEN: same command → **16 passed in 0.08s** (0.17s after format).
- `uv run ruff check` on touched files: **All checks passed**;
  `uv run ruff format`: clean (1 file reformatted, re-verified green).

## Dependent change (reported, NOT edited — proxy.py reserved)

A truthfully-FRESH-input display needs `proxy.py::_record_usage` to feed the
history ring `in_=usage["input"]` (fresh only) or a separate ring field —
that is a SEPARATE dependent change to a reserved file. Until then the gauge
keeps its honest TOTAL meaning and now says so in its label.

## Non-claims

No runtime/registry activation, no restarts/deploy/provider calls, no heavy
suite/build. This is the independent conservation fix only.
