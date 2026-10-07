# Tasks — independent browser QA

- [x] Read team scope, pinned Impeccable, browser INDEX/Cookbook, existing checks.
- [x] Verify isolated `319-ui-browser-qa` worktree and exact #316 base.
- [x] Capture admission refusal and inspect sanctioned off-host availability.
- [x] Capture sanitized old live DOM, units, source/build receipts (17:46 BRT).
- [x] Extend existing minimal runnable browser check, not app implementation.
- [x] Execute admitted desktop+narrow, HTMX, keyboard/focus, state/a11y checks:
  18:09 initial + 18:21 one confirmation. Layout FAIL retained (390→473px);
  other exercised source/state/network checks pass. Not post-deploy acceptance.
- [x] Run source acceptance and Ruff/full pytest via normal CI: exact 53578a2,
  run 37685735402, 2,064 passed; browser remains unrun.
- [x] Publish durable report and receipts through normal hooks/PR #318.
- [x] Merge source-only slice after every current-head required gate is green:
  #318 MERGED `1511eb05`, exact head `3d79c87`; CI 37686470563 and scan 37686470589.
- [ ] Verify exact post-deploy revision or record precise runtime-owner blocker.

Unchecked executable gates remain unaccepted on refusal; no synthetic-to-live
promotion. Evidence and check status are recorded in the owned report.
