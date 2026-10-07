# Gauge caption operability — 07/10/2026

Owner `thr-ui-frontend`; isolated branch/worktree `317-ui-operability`. Generator OpenAI, pinned GPT-6.1 Sol; no spawned agents, fallback, reviewer verdict or deployment claim.

## Evidence / smallest slice
Base #316: `66ad9dc9df82887bccc90572ae0d84dadf5df5fe`, `gh pr view 316` = `MERGED`. Read exact source falsifier and checks from sibling `320-ui-tooling-research/docs/UI-TOOLING-RESEARCH-2026-10-07.md`. Date verified **07/10/2026 17:54 BRT**.

Hypothesis: the normal-size `.tps-foot` caption uses a decorative colour on the mantle panel, making its sampling/freshness and throughput-not-quota context illegible; changing only its token fixes the objectively reproduced source pair. Falsifiers: actual selector-derived ratio already ≥4.5:1, or replacement fails on actual panel surface.

Independently reproduced with existing contrast arithmetic, reading the actual `.tps-foot` colour and `.tps-panel` background:
- Before: `--ctp-overlay0` `#6c7086` on `--panel` → mantle `#181825`: **3.5943:1**; fails normal-text AA 4.5:1.
- After: existing semantic `--muted` → `#a6adc8`: **7.8856:1**; passes that same source surface.

Receipts: `docs/evidence/ui-operability-2026-10-07/contrast-source-{before,after}.txt`. These are source arithmetic, **not pytest, computed-browser contrast, live accessibility or runtime delivery**. Initial targeted pytest call was refused before execution (`pressure-high`, memory-full=4c, io-full=1047c); red pytest is not claimed yet.

## Implementation / spec
One CSS replacement: `.tps-foot` colour → `var(--muted)`. One red-capable selector/surface regression in `tests/test_ui_contrast.py`, using existing alias/luminance functions and 4.5:1 floor. It reads the real rules, so the gate cannot merely validate an unused palette list. No template/layout/backend/collector/runtime or gauge unit/freshness/tab-state change. Decorative `overlay0` uses remain unchanged.

Speckit source plan/tasks: `specs/317-ui-operability/`. Pedro's 17:53 BRT instruction explicitly authorizes this objective source fix without a screenshot prerequisite. Navigation was not implemented and is optional later debt: original unexecuted probe preserved as **untracked WIP in this feature worktree** at `specs/317-ui-operability/optional-source-navigation.py` (not included in the contrast PR). Preserve this worktree while that optional file exists. It is not this slice's acceptance or a CI bypass.

## Tool / acceptance state
Versioned Impeccable 4.5.0 context ran once; adapt/craft-floor guidance read, craft-floor reread before CSS edit. Local engine probe **0.1.11**, SHA-256 **0221607e1f535af937ea267c347b1f90233b85dc2563cbd2eeefdf42e0e5c594**, matches campaign. Detector ran once on finished CSS (exit 0): **five existing side-tab accent-border warnings**, at lines 210/895/1015/1016/1021; none in the changed caption rule (1150). Kept incumbent status/config surfaces rather than expanding this one-token fix or suppressing findings. Receipt `impeccable-detect.json`. This is static advice, not a WCAG/browser verdict. Ruff lint/format and `git diff --check` passed in the bounded control lane (115 files formatted; `ruff.txt`). Targeted/full pytest plus baseline-regression assertion requests were refused in the heavy lane: `reopen-pending`, then `io-full=1176c`, then `io-full=1818c`; no local pytest/regression execution is claimed. Normal commit hooks passed: code-slop 100/zero errors, ratchet zero regressions; alignment zero errors/warnings (`commit-hook.txt`). Source PR **#320**: https://github.com/yolo-labz/anthropic-throttle-proxy/pull/320 ; initial head `f41c277c8ed715e453593929d63f47fec6e3c898`. Initial exact-head CI has quality/CodeQL/image/dependency/type-report checks green; full pytest and scan still running (`ci-initial.json`). Required remote CI, not a small-lane test fallback, supplies full-suite acceptance if green. Desktop user-bus refused detached waiter creation; the bounded CI-only waiter is on Nix.Server (`thr-ui-317-ci-watch`, 128MiB/10% CPU/600s), not a remote test/build or operator browser. Its machine results will be copied into repo evidence; runtime state is not the only durable copy. No model review approval fabricated.

Earlier navigation/browser admission history remains in `docs/evidence/ui-operability-2026-10-07/blocker.json`; it is historical pre-fix scope, not current acceptance. Existing screenshots in spec 289 predate #316's picker and are not new/live evidence.

## Coordination / runtime boundary
QA seat owns browser-rendered narrow/desktop, keyboard/tab polling, errors and real producer acceptance. Runtime owns gauge producer/pin/activation; deployment is not attempted here. Sibling QA/runtime source files untouched. Source arithmetic fix may land through required green checks; **browser-rendered/live acceptance remains explicitly pending**. Coordinator `w1P:pG` consumes durable source/campaign receipts, not pane directives.

Source rollback after merge: one normal revert PR of the squash commit. No live runtime changed in this seat.
