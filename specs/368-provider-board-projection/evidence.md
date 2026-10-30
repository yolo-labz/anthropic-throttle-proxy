# Executable acceptance

30/10/2026. Generator: GPT-6.1 Sol (OpenAI); no worker/reviewer jobs. Effort setting not independently exposed by environment.

## Artifact isolation

Tests ran from a clean `git archive origin/main` with only owned source/tests/example overlaid. `PYTHONPATH="$isolated/src"` and the existing venv interpreter avoid the shared editable-install trap. Unrelated dirty limiter/config/conftest files and concurrent visual changes are excluded. Exact-branch targeted acceptance also passed 108 tests plus ruff after selective commit. The shared target is behind main: the initial exact-branch full run inherited seven obsolete MiMo fixture failures and a durable-cleanup deadline failure already fixed upstream. It also exposed stale materialization of three owned files by the initial selective commit; accepted copies were restored, committed separately and byte-compared with the passing overlay. Parent coordination is required before target synchronization; no uncoordinated `but pull` was run.

## Results

- Regression before implementation: `pytest tests/test_provider_board_projection.py -q --tb=short`: exit 1, six failures (including ghost sibling providers and missing validation).
- Initial implementation check: one test-fixture failure (`index.html` is not the dashboard template). Follow-up fixture check required the real handler's `asset_v`; no application behavior was weakened. Attempt 1 implemented projection; fixture corrections only, no provider retries. Attempt 2 added the blank-name guard after a red regression showed whitespace-only destination names could match absent fields and blank the board.
- Targeted config/display/retirement/data/enrollment/capacity tests: exit 0, **108 passed**; offline dashboard + repeated stats rendering included.
- Full `pytest -q --tb=short`: exit 0, **2400 passed**, 276 existing warnings.
- `ruff check src tests`: exit 0. `ruff format --check src tests`: exit 0, 155 files formatted.
- `alignment-gate --worktree`: exit 0.
- Installed `code-slop-gate` does not support `--worktree` (exit 2). Default repo-wide invocation exposes existing debt (one error, 42.39% duplication). Supported path-scoped invocations for all four owned Python files pass, each with zero errors/warnings/duplication; no thresholds changed or hooks skipped.

## Runtime boundary and reversal

Candidate YAML was accepted by the actual deployed package's config loader, backed up durably, and replaced only after exact-byte race checks. Only display configuration changed. Same gateway PID and immutable build remain; no package/service activation. The old package ignores the new `hidden_providers` setting: full routing/registry/workload projection is source-only until authorized deployment. Live rendered inspection belongs to the separate browser owner; all subsequent rendering acceptance here is offline.

YAML rollback: restore the durable `provider-ui-fleet-ui.before.yaml` after checking for newer operator edits. Source reversal: one revert PR. Detailed private runtime receipts and hashes live in the studio's vault evidence folder, not scratch.
