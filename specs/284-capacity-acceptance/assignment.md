# Executable seat-capacity acceptance — workspace w1P

## Mission
Author deterministic regression/acceptance tests for pooled seats, queue fairness and concurrent useful progress. This is test implementation, NOT an independent-family model review or approval gate.

## Ownership
Work only in `/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-284-capacity-acceptance`, branch `284-capacity-acceptance`, base `2e7a43f`. Own ONLY `tests/test_seat_capacity_acceptance.py` and `specs/284-capacity-acceptance/`. Other workers change production routing, meters and UI separately. Do not edit their branches/files or start extra workers.

## Acceptance matrix
Reuse existing pytest/aiohttp helpers and local fake upstreams, never real providers. Cover static-key pool retirement A→B while B is busy; concurrent requests spanning two eligible accounts; one exhausted/rejected account not consuming useful slots; differing caps and durations; queued cancellation returning leases; measured per-client fair access without exceeding caps. Find the existing tested mechanisms first and add only missing high-value coverage, not duplicate the suite. Separate baseline failures exposing the defect from unrelated fixture errors. No test may pass by swallowing failures, skipping assertions or depending on real credentials. Assert actual successful completions, not just task creation.

Follow minimal speckit plan/tasks. Run your tests and ruff; preserve baseline failures as evidence if awaiting production fix. No model-judgment claim, live inference, browser, rbw, private vault or deployment. Commit only your files, with normal hooks; no push/merge. Write `result.md` with exact command outcomes and SHA. Coordinator integrates and reruns against exact combined HEAD in 279.
