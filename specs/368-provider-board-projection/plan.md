# Plan

1. Read owned Python/config surfaces and current YAML; inspect immutable deployed source and JSON health only.
2. Reproduce ghost providers on a clean archive of origin/main with explicit PYTHONPATH (not the shared editable venv import target).
3. Extend existing config validation with explicit provider hiding; share the predicate across registry/routing/workload presentation. Preserve per-account independence.
4. Update example/inventory docs; validate the candidate YAML with deployed code, preserve durable rollback bytes/hash, compare current file for races, and hot-reload display configuration only.
5. Run targeted tests, full pytest, ruff and commit gates on the isolated feature artifact. Selectively publish own files to a PR and follow required CI within the 30-minute/two-attempt bound.

No new dependencies, provider probes, workers, backend policy, template edits or package activation. Follow the brief's shared GitButler workflow instead of the superseded worktree-first recipe. Isolated archive supplies clean executable acceptance without mutating sibling work.
