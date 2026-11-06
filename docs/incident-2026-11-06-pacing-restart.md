# Weekly pacing restart — 06/11/2026

## Symptom and hypothesis

Pi received `throttle_timeout: proxy queue wait exceeded; slots saturated`.
The live gateway remained on `b679b641`; source changes alone cannot repair it.

Hypothesis: losing the plateau in process-local weekly samples on restart makes
one coarse meter step look like excessive burn, creating a false fleet-wide
pacing deferral. The incident replay in the original review reproduced 13
false defer minutes (15:20–15:32 BRT); restoring persisted history eliminated
those minutes. This is evidence for that failure mechanism, not proof that all
queue timeouts have the same cause. A separate DNS outage is not addressed.

Falsifier: restoring the same-reset plateau leaves the replay verdict unchanged,
or restored history hides subsequent real consumption after recalibration.

## Correction and safety

- Seed weekly burn samples before listening, from the current recorder log and
  its `.1` rotation within one combined 8 MiB read allowance.
- Ignore stale/future/expired readings. Segment reset epochs and downward meter
  recalibrations before downsampling, both during restoration and live updates.
- Reuse a committed SSE hold for consecutive pacing/share waits.
- Spend one inherited end-to-end deadline across pacing, shares and slot waits.
- Keep expiry fail-closed and log it. **No admit-on-expiry, renewed queue budget,
  relaxed pacing threshold, cooldown bypass or extra upstream capacity.**

The initial proposal's expiry admission and renewed queue allowance were removed
in response to the Codex adversarial review. The other two findings—meter
recalibration and log rotation—have direct regression tests.

## Executable evidence

From this branch before integration with #18:

```sh
uv run pytest -q tests/test_pacing_restart_safety.py --tb=short
uv run pytest -q tests/test_usage_allocator.py tests/test_meter_recorder.py tests/test_proxy_app.py
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
code-slop-gate --worktree
alignment-gate --worktree
```

- New safety tests before correction: **4 failed, 2 passed** (three recalibration
  variants and rotation exposed the reviewed defects).
- Focused restart/pacing/inherited-budget selection: **21 passed**.
- Full suite: **2658 passed**; lint, format and both gates pass.
- Reintroducing a fresh queue deadline after pacing makes
  `test_pacing_cannot_renew_an_inherited_queue_budget` fail by serving the upstream
  message after the inherited budget expired; corrected code passes.
- Additional cross-family review was attempted once but refused before spawn:
  Z.AI lane saturated. This is not a successful review or an approval.

## Delivery / runtime boundary

#18 (hard eligibility before pacing spill) was independently merged as
`48a5955fdec2684f52eddabd52eb3989c5f1d990`. Combined-source validation after
resolving the two overlapping handler edits: **2677 passed**, Ruff lint/format
clean. Both model-aware hard eligibility and the single inherited deadline
remain in the combined handler. The managed pin and live one-line SSE repair are
distinct from these source changes. No activation, restart, business-pane retry, credential
change or relaxed account guard is authorized by this evidence note.

After an authorized one-service deployment: check effective and persisted unit
paths, health `.build`, startup `week-samples-seeded`, both accounts' usable
current-epoch sample coverage, and real Pi text/tool/continuity acceptance.
Do not declare the incident solved before that runtime verification. Keep the
existing SSE decoding repair in the deployed ancestry. Reversal is one normal
revert PR per squash commit; runtime rollback uses the previously verified
immutable package only in the authorized deployment window.
