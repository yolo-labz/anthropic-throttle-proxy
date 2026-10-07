# Tasks

- [x] T1 Read campaign, versioned skill/context and #316; read exact tooling contrast falsifier and trace actual caption/panel CSS.
- [x] T2 Add selector/surface regression; independent source arithmetic reproduces baseline 3.5943:1 < 4.5:1 (`contrast-source-before.txt`). Initial red pytest admission refused, so not claimed.
- [x] T3 Change only `.tps-foot` to `--muted`; independent arithmetic is now 7.8856:1 (`contrast-source-after.txt`).
- [ ] T4 Verify full pytest through exact-head required CI (local heavy test admission refused); Ruff and one versioned detector pass are captured.
- [ ] T5 Deliver exact-head normal hooks/PR/CI, verify merge, update source/campaign receipts.

## Optional later debt (not this slice's acceptance)
`specs/317-ui-operability/optional-source-navigation.py`: prepared but unexecuted prior navigation probe, preserved as untracked WIP in the owned feature worktree (not staged for this contrast PR). Its 44px/skip-link changes are not implemented. Before/after browser screenshots, native focus and per-tab polling acceptance remain later work requiring admitted browser capacity. Browser-rendered/live caption contrast also remains pending with QA/runtime owners.
