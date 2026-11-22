# Native history and savings evidence

`scripts/token-usage-lake/research/history_audit.py` creates an offline, content-free census of
retained native Pi assistant usage and compactions. It reads every `.pi/agent*/sessions` root
and Pi recovery archives, including account-specific roots. It never calls a provider, edits a
session, changes a model, or starts another ingestion service.

```bash
umask 077
python3 scripts/token-usage-lake/research/history_audit.py \
  --cutoff '2026-11-14T18:06:00-03:00' \
  --output /private/evidence/history-audit
uv run pytest -q tests/test_history_audit.py
```

Choose a new private output directory for every freeze. The command refuses to overwrite a
previous generation. It writes `events.sqlite`, `daily.json`, `summary.json`, `sources.jsonl`
and `sha256.json`. Keep these private; do not commit real datasets. An interrupted scan is
**not** a result: only a completed generation has the final manifest. The manifest includes
the analysis source hash. Retain that source version alongside published results.

## Accounting contract

- Read each source to its starting byte boundary. Record its prefix hash and concurrent mutation.
  This is not an atomic fleet snapshot or proof of complete provider capture.
- Deduplicate native event identity across copied files and forked sessions. Quarantine conflicting
  copies instead of picking the cheaper observation. Retain failures and aborts.
- Missing, negative, nonfinite and boolean usage is unknown, not zero. Report measurement coverage
  separately. A full-population fraction is null when the necessary recorded measurements are missing.
- Reasoning is already included in native output. Never add it again. Record native-total versus
  component-total discrepancies. Token categories are not interchangeable prices or quota units.
- Compaction attribution is the latest native model selection, not an attested compactor wire model.
  Assistant attribution comes from the native assistant record. Unknown labels stay unknown.
- Absent daily bins are unobserved, not zero. Days use UTC. Different model eras are not a randomized
  model comparison. Other clients, external calls, gateway receipts and billing are outside this census.

## Decision contract

The hypothetical output cap is **arithmetic**, not a summary-text cap or a deployed treatment.
Compare its removed output with the whole observed workload, not only the compaction subset.
The observed-output ratio remains descriptive when coverage is incomplete; its full-population
counterpart stays null. Neither is realized savings. Downstream cache changes, failed tasks and
rework can reverse an apparent gain.

Do not infer usefulness from a forecast score, identifier retention, a commit, a note, or `end_turn`.
An intervention needs independently accepted tasks and total cost through completion, including
failures, retries and repairs. Keep model and effort fixed. Freeze assignment, quality thresholds,
observation horizon, meaningful effect and primary endpoint before enrollment. Use independent
task/lineage units, cluster-aware uncertainty and a future holdout. A confidence interval crossing
zero is inconclusive; it does not demonstrate no effect.
