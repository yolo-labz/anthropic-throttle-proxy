# Independent UI/browser acceptance — 07/10/2026

Seat: `thr-ui-browser-qa`, w1P:pW. Generator: OpenAI GPT-6.1 Sol.
Coordinator: w1P:pG. Isolated branch: `319-ui-browser-qa`.

## Scope and status

**Browser acceptance is FAIL at 390px; post-deploy acceptance remains blocked.**
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

- Desktop effective ExecStart and final persistent drop-in name that package;
  receipt includes persistent symlink/drop-in chain without environment secrets.
  Runtime owner separately found an older base HM unit masked by the persistent
  incident drop-in; base unit alone is not the running build proof.
- Actual gauge: **“Output throughput not yet measured”**, numeric **“—”**.
  No source-picker links; HTMX polls `/ui/stats` without per-tab source selection.
- Default health omits `throughput`. **Correction after testing the opt-in**:
  `/__throttle/health?telemetry=1` on all three old instances does publish
  `bucket_seconds=10.0`, 360 `[output,input+cache]` buckets, all zero, with no
  `tokens_fresh` sidecar. Default-schema absence is not missing producer support.
  Neither served counts nor quota percentages establish current stream speed;
  zero accounting history is not a fresh inference probe.
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
for unknown/stale/error fixtures. At **18:00 BRT**, explicit opt-in live telemetry
confirmed the same 10-second/two-column units, but no accounted tokens in any
retained bucket. Old builds lack the fresh-input sidecar. Synthetic 120 tokens/s
is **not live MiMo acceptance**. Receipt: `telemetry-optin.json`.

Runtime seat's proposed independent contract is
`THROTTLE_MIMO_DESKTOP_REPORT`, one `mimo:desktop-subscription` row with weekly
`remainingPercent`, `usedPercent=100-remainingPercent`, `unit=percent` and optional
reset epoch. Monthly Token Plan credits/seat credits are different units/windows;
no allowance, billing, throughput or pace should be inferred from weekly percent.
At baseline capture that producer/reader and frontend work were still uncommitted
in their own worktrees; no latest-runtime or frontend acceptance is claimed.
The runtime owner's allowlisted producer receipt at **17:52 BRT** reports
**93.3% weekly remaining / 6.7% used**, `limitId=weekly`, `unit=percent`, reset epoch
1791844029. Its producer maps the bridge's `percent` to remaining, not used.
That observed producer report is not yet a deployed `/ui` row or continuous
sampling proof; no raw credential-bearing API payload was copied. Runtime source
head observed at 18:00 BRT: `0add715ab1e59d5e48ada15194c99ada2e8314e2`;
frontend later committed its caption-only contrast fix at
`f41c277c8ed715e453593929d63f47fec6e3c898` (#320). The **18:05 BRT** exact-source
comparison is `latest-source-state.json`; live UI still differs from #316 and
the frontend commit. Runtime source remains `0add715a` (#319), with a subsequent
uncommitted reader repair. Live imported build remains `jmak9s5h…`; no
post-deploy acceptance is claimed.

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

## Admitted browser evidence — 18:09 BRT

Heavy admission finally granted. Installed headless Chromium **152.0.7977.82**,
GPU disabled, ephemeral owned page; job memory peak **~1 GiB**, CPU time 8.59 s,
wall time 10.89 s. No supervised account/profile or service mutation.

- **Actual old live** `/ui` at 1366 and 390 CSS pixels: document width equals
  viewport; one main/h1, unique IDs/named controls; no gauge text clipping.
  Fresh sanitized `live-{1366,390}.png/.html` captured, independent of the
  coordinator screenshot. This proves the old page's layout, not deployment.
- **Exact #316 route**, synthetic workload buckets **plus the existing read-only
  lane-report snapshot** (not wholly synthetic account quotas): gauge 120 output
  tokens/s; actual HTMX polls retained `source=mimo`; source aria-current remained
  selected; checkbox selection and `providers-scroll` focus survived another poll.
- **FAIL:** at 390 pixels, document width **473 pixels** (83-pixel page overflow).
  1366-pixel workload view has no page overflow; no gauge text clipping, duplicate
  IDs or unnamed inputs in either viewport. Source anchors measured **16.875px
  high** (25/30px wide). Checkbox geometry is 13px, but its wrapping label must
  be considered before calling the whole target a failure.
- Failing DOM/PNG and exact imported-source hashes are durable. First pass stopped
  at overflow; stale/error/unknown transitions, touch/keyboard-source selection and
  final console/network acceptance were **not yet reached**. Earlier executed
  selection/focus assertions are not promoted to a full pass.

Hypothesis for the failure: the workload page's capacity/metadata layout overflows
when a lane report exists; neither the gauge itself nor the new source picker is
established as the cause. Falsifier: a bounded rectangle census locates overflow
elsewhere. QA makes no competing template/CSS fix. Batched confirmation retains
all failures and nonzero exit, but collects later checks instead of stopping at
the first layout error. One remaining confirmation attempt at 18:11 was refused
because the shared heavy slot was running since 18:09:42; no unadmitted fallback.

## Bounded confirmation — 18:21 BRT

Exactly one remaining browser pass ran after #319/#320 source landed. Branch
`42f9ba8` includes runtime squash **1969bdcb** and caption squash **4aedd3fc**.
Targeted local tests: **104 passed**, one warning, 1.67 seconds. Browser job:
15.75-second wall time, 10.96 CPU seconds, **620 MiB** peak; no further polishing
or browser loop. Screenshot/DOM/receipt: `confirmation/` in the evidence folder.

- **PASS, synthetic workload + real read-only lane report:** HTMX source remains
  `mimo`, gauge 120 tokens/s, selected native detail checkbox retained; focused
  `subs-scroll` survives polling with computed 2px visible outline/box-shadow.
- **PASS:** stale 60-second sibling and failed sibling show throughput unavailable
  without a numeric sweep; local cold history says unknown/“—”; keyboard Enter
  selects local and Chromium-synthesized touch selects the sibling link.
- **PASS:** structural landmark/control-name/ID checks; no gauge text clipping;
  **zero page/console errors**, **zero unexpected browser network requests**.
  Cached route rendering cannot invoke collectors or `ClientSession._request`.
- **FAIL retained:** 390px workload document width **473px** after both sibling
  changes; final error/sibling view measured **1391px at 1366px**, **473px at 390px**.
  The final receipt's historical key `unknown_view_overflow` actually describes
  the returned **failed sibling** page after the touch link, not the local unknown
  page. Raw widths/screenshots are preserved; no renamed result hides a failure.
- Overflow census found **no outside-table bounding box beyond the viewport**;
  scroll-region descendants were intentionally excluded. This falsifies the
  earlier outside-capacity-metadata attribution. Root cause is **unverified**;
  overflow propagation from the scroll region remains a candidate, not a shipped
  fix. Do not patch the picker/gauge purely by symptom similarity.
- The old **live** page still fits both widths. Running package remains `jmak9s5h…`,
  so none of the changed-source checks is a post-deployment pass. Central revision
  was not verified or activated by QA. Runtime owner still owns that transition.

The check exits **1**, after exercising all checks, because the recorded layout
findings remain. No runtime/template/CSS fix or acceptance waiver was made.

## Execution evidence and exact blockers

- Initial heavy browser smoke: refused before execution, **exit 75**,
  `pressure-high: cause=io memory-full=0c io-full=1125c`.
- Combined confirmation (deps/Ruff/targeted tests/browser/full pytest): not started,
  **exit 1**, `Unit desktop-job-slot.service was already loaded`.
- Later attempts also refused before execution: `reopen-pending` (23 seconds of
  below-threshold recovery remaining), another loaded-slot collision, then
  `pressure-high: io-full=1453c`. No browser/test subprocess launched in those
  refused attempts; the two later admitted passes are recorded separately above.
- At 17:48 BRT that unit was active/running since **17:43:15**, CPUQuota **4s**,
  MemoryMax **4 GiB**. No killing/resizing the sibling workload, sleeping inside a
  job, small-lane test/render fallback or global cache/worktree sweep.
- Sanctioned off-host inventory: Nix.Server pod pool/MCP inactive since 05/10/2026;
  no current CPU allocation, browser cache absent. No restart/alternate allocation
  was invented. Browser refusal is not visual acceptance.
- Lightweight source lint under the admitted control allocation:
  **Ruff check src tests passed; 116 files already formatted**;
  `git diff --check` passed. Source pytest/sanitizer
  did not execute locally before the admitted first browser run. CI executed
  the sanitizer/full source suite; actual browser results are recorded above.

## Delivery and deployment

PR **#318**: https://github.com/yolo-labz/anthropic-throttle-proxy/pull/318.
Normal hooks passed (code-slop/alignment); no hook bypass. On exact source head
`53578a21cc50eeca2fda61321a971cdc28e365f9`, CI run **37685735402** completed
successfully at **17:57 BRT**: Ruff checks plus **2,064 passed**, 155 warnings,
84.28 seconds. The sanitizer regression was executed there. This is source
acceptance only; the browser script is not a pytest test and did not run in CI.
At that historical observation required `scan` was still pending. On final source
head **`3d79c879519445d16c7697e1abb0ae7a85d39ec8`**, CI **37686470563** passed
**2,064 tests** (84.38 seconds), Ruff and format. All required checks passed:
`ruff + pytest`, `code-slop + alignment`, and `scan` (run **37686470589**).
`gh pr view` showed `CLEAN`, no required approvals, and GraphQL returned no review
threads. **#318 MERGED at 18:06 BRT** with squash
**`1511eb05fdafdd6f8e739e7389dd71384613aef5`**. Receipts are `delivery-318.json`,
`required-checks-3d79c87.json`, `ci-3d79c87.json`. No source-green-to-browser/live
promotion. The initial wrong-number #319 waiter was immediately cancelled with
no changes or merge intent.

Runtime owner must land/activate its exact revision first. Re-read
running import path, persisted/effective unit, configured report shape and served
UI asset/source digests, then perform admitted post-deploy browser checks. A green
source PR alone cannot close this runtime/browser gate.

Rollback for the check-only slice: `git revert 1511eb05` in a normal PR. No service
rollback is needed because this seat changes no service.
