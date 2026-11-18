# Plan — spec 377 phase 1

1. Pure rules in `src/anthropic_throttle_proxy/usage_allocator.py` (no I/O, no proxy import).
   Functions: `allowed_7d`, `account_state`, `catchup_wait_s`, `decide_pacing`,
   `decide_client_share`, `publish`; parsers `parse_mode/parse_burst/parse_shares`.
2. Config knobs in `config.py`: `WEEKLY_PACING_MODE`, `WEEKLY_BURST`, `CLIENT_SHARES` (fail-closed parsing).
3. Proxy wiring in `proxy.py`:
   - `_weekly_pacing_filter` (spill-only), called where the routing snapshot is taken.
   - `_weekly_pacing_defer` + `_weekly_pacing_response`, called before `_claim_route`
     (no probe claimed, no slot taken; uses the existing stamped 503).
   - `_weekly_pacing_view` added to `/__throttle/admission`.
4. Tests: `tests/test_usage_allocator.py` (pure rules, A1–A4, parsing, finite keys) and
   `tests/test_usage_allocator_wiring.py` (proxy functions with a fake snapshot).
5. Mutants: `burst := inf`, `Retry-After := 5` must each fail the suite.
6. Gates: targeted tests, ruff check + format, code-slop-gate, alignment-gate, PR CI.
