# Review of 282 — executable policy claims

Generator: MiMo (Chinese-frontier); reviewing coordinator: GPT/Astra (OpenAI). This is a source review, not a fabricated external provider verdict.

Concrete findings on `19313dbb5783f67287a7518dd6fd18e6544dc57e`:

1. `add_window_percents` permits addition of percentages whenever window names match. Across two equal-size seats, 40% and 30% means 35% of their combined allowance, NOT 70%. Equal window labels also do not prove common boundaries/units/denominators. Remove this misleading helper/example, or demonstrate genuinely compatible weighted raw-current/raw-limit aggregation with strict window/units identity. Simpler: document that unlike or unbound denominators must not be aggregated at all.
2. `active_quota` returns concurrency slots, not subscription quota. Rename the metric to eligible slots and stop calling it active quota. Quota/slots/throughput are distinct dimensions.
3. Phase 2 manually constructs assignments from a toy roster. Zero jobs on Z does not verify the production router excludes unavailable accounts. Label this as a scheduling illustration, not F3 production acceptance; the dedicated 284 tests own actual handler/router evidence. Likewise phase-6 hand-written helpers are not guards in the running system.
4. Result says 21/21 assertions but the executable should emit its own assertion count, or avoid a manually maintained number. Run ruff format as well as lint.
5. Replace the report's `git checkout SHA -- .` rerun recipe: it destructively overwrites the current worktree. The already checked-out isolated branch only needs `uv run python scripts/check-seat-fairness.py`.

Real-limiter RR, cancellation, bounded wait and AIMD-floor phases are useful and should remain. Keep the fix within the assigned files; no production scheduler changes or new framework.
