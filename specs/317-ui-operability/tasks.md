# Tasks

- [x] T1 Read campaign, versioned skill/context and #316; read exact tooling contrast falsifier and trace actual caption/panel CSS.
- [x] T2 Add selector/surface regression; independent source arithmetic reproduces baseline 3.5943:1 < 4.5:1 (`contrast-source-before.txt`). Initial red pytest admission refused, so not claimed.
- [x] T3 Change only `.tps-foot` to `--muted`; independent arithmetic is now 7.8856:1 (`contrast-source-after.txt`).
- [x] T4 Source-identical receipt head `1c73425`: CI run `37687007287` passed Ruff/full pytest (2,060 passed); local heavy tests refused. One versioned detector pass captured (five existing, unrelated warnings).
- [ ] T5 Normal hooks/PR #320 delivered; verify final receipt-head required CI and merge. Final delivery receipt is recorded in the campaign save-state rather than asserting a future merge here.

## Optional later debt (not this slice's acceptance)
`specs/317-ui-operability/optional-source-navigation.py`: prepared but unexecuted prior navigation probe, preserved as untracked WIP in the owned feature worktree (not staged for this contrast PR). Its 44px/skip-link changes are not implemented. Before/after browser screenshots, native focus and per-tab polling acceptance remain later work requiring admitted browser capacity. Browser-rendered/live caption contrast also remains pending with QA/runtime owners.
