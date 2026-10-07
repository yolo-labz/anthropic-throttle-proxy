# Independent UI/browser acceptance — 07/10/2026

Seat: `thr-ui-browser-qa`, w1P:pW. Generator: OpenAI GPT-6.1 Sol.
Coordinator: w1P:pG. Isolated branch: `319-ui-browser-qa`.

## Scope and status

**Browser acceptance and post-deploy acceptance remain blocked, not passed.**
This slice changes only the existing executable check, its redaction regression,
plan/tasks and receipts. No competing templates/backend edits, runtime changes,
agents, account attachment or provider fallback.

Plan/tasks: `specs/319-ui-browser-qa/`. Hypothesis and falsifiers are in the plan.
Versioned Impeccable **4.5.0 / engine 0.1.11** was explicitly read; `context` ran
once (existing narrow audit allowed). Canonical browser INDEX/Cookbook and the
existing spec-227/gauge checks were read. No new dependency was added.

## Old live desktop: measured, not synthetic

At **17:46 BRT**, read-only `/ui`, `/ui/stats`, `/__throttle/health` returned HTTP
200 on `:8765`, `:8766`, `:8773`. All imported:

`/nix/store/jmak9s5hhv33bp53npypcnlkcrjn2cxz-anthropic-throttle-proxy-0.1.0/lib/python3.14/site-packages/anthropic_throttle_proxy`

- Desktop effective/persisted ExecStart both name that package; receipt includes
  persistent symlink and drop-in paths without environment secrets.
- Actual gauge: **“Output throughput not yet measured”**, numeric **“—”**.
  No source-picker links; HTMX polls `/ui/stats` without per-tab source selection.
- Health exposes **no `throughput` payload** on these old builds. Neither served
  counts nor quota percentages establish throughput; absence is not measured zero.
- Six UI files differ from exact #316 `66ad9dc9df82887bccc90572ae0d84dadf5df5fe`.
  Per-file SHA-256 comparison is durable in the build receipt, not an inferred
  Git revision from a package basename.
- Current live asset revision is **`f3847b8fb076`**. The coordinator's incumbent
  `ui-operator-baseline.png` has that same footer revision and visible 17:09
  render stamp. It was inspected as provided incumbent visual evidence, **not**
  claimed as this seat's fresh screenshot. It remains in the canonical vault,
  not copied with its account identity into repository evidence.

Durable evidence: `docs/evidence/ui-browser-qa-2026-10-07/old-live-source-build.json`
and `old-live-{8765,8766,8773}.html`. DOM receipts redact emails, bearer hashes
and monetary amounts. No raw credentials/usage API response was retained.

## Units and integration seam

Exact #316 accepts sibling **10-second token buckets**: `[output, input+cache]`,
optional separate measured fresh-input buckets. Six synthetic `[1200,3000]`
buckets imply **120 output tokens/s**, not quota or an instantaneous stream rate.
The check asserts that visible/accessibility label and removes the numeric sweep
for unknown/stale/error fixtures. The live producer does not yet provide that
payload, so synthetic 120 tokens/s is **not live MiMo acceptance**.

Runtime seat's proposed independent contract is
`THROTTLE_MIMO_DESKTOP_REPORT`, one `mimo:desktop-subscription` row with weekly
`remainingPercent`, `usedPercent=100-remainingPercent`, `unit=percent` and optional
reset epoch. Monthly Token Plan credits/seat credits are different units/windows;
no allowance, billing, throughput or pace should be inferred from weekly percent.
At baseline capture that producer/reader and frontend work were still uncommitted
in their own worktrees; no latest-runtime or frontend acceptance is claimed.

## Minimal runnable check

Reuse: `tests/check_gauge_wire.py`; default output is the durable evidence folder.
Under an admitted heavy allocation:

```sh
BROWSER_EXECUTABLE=$(command -v google-chrome) \
  uv run --frozen --group delivery python tests/check_gauge_wire.py
```

On an already allocated CI/off-host runner use `--synthetic-only --out <durable-dir>`.
This is not permission to launch an unallocated browser. Existing Playwright is
already a declared delivery dependency; no browser downloads/profile takeover.
One ephemeral headless Chromium page, GPU disabled, sequential widths 1366/390.

The executable asserts actual HTMX polls retain source selection; native keyboard
Enter selects the source; checkbox preference and focusable region ID survive
polling; native touch tap selects a source; stale/error/unknown readings have no
numeric sweep; page/gauge text do not overflow; named inputs, unique IDs and
main/h1 landmarks exist; cached rendering cannot call any `ClientSession._request`
or collector. Console exceptions/errors and unexpected external requests fail.
Expected HTMX 1.9.12 CDN loading is distinguished from provider API traffic.
Sanitized viewport PNG/DOM and UI source hashes are written when execution occurs.

This is **partial a11y coverage**, not a WCAG certification or automated approval.
Small targets/focus styles are measured; existing contrast tests remain the color
oracle. Physical touch, screen readers, all 200% zoom/device variants and live
provider-network accounting require separate evidence. Tests do not prove them.

## Execution evidence and exact blockers

- Initial heavy browser smoke: refused before execution, **exit 75**,
  `pressure-high: cause=io memory-full=0c io-full=1125c`.
- Combined confirmation (deps/Ruff/targeted tests/browser/full pytest): not started,
  **exit 1**, `Unit desktop-job-slot.service was already loaded`.
- Later attempts also refused before execution: `reopen-pending` (23 seconds of
  below-threshold recovery remaining), another loaded-slot collision, then
  `pressure-high: io-full=1453c`. No browser/test subprocess ever launched.
- At 17:48 BRT that unit was active/running since **17:43:15**, CPUQuota **4s**,
  MemoryMax **4 GiB**. No killing/resizing the sibling workload, sleeping inside a
  job, small-lane test/render fallback or global cache/worktree sweep.
- Sanctioned off-host inventory: Nix.Server pod pool/MCP inactive since 05/10/2026;
  no current CPU allocation, browser cache absent. No restart/alternate allocation
  was invented. Browser refusal is not visual acceptance.
- Lightweight source lint under the admitted control allocation:
  **Ruff check src tests passed; 116 files already formatted**;
  `git diff --check` passed. Source pytest/sanitizer
  and the actual browser check have **not yet executed locally**.

## Delivery and deployment

Normal hooks/PR/CI are the source delivery path; exact-head CI receipt will be
recorded here. Runtime owner must land/activate its exact revision first. Re-read
running import path, persisted/effective unit, configured report shape and served
UI asset/source digests, then perform admitted post-deploy browser checks. A green
source PR alone cannot close this runtime/browser gate.

Rollback for this check-only slice: one `git revert <squash-commit>` PR. No service
rollback is needed because this seat changes no service.
