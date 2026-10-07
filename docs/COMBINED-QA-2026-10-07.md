# Independent combined-output QA — 07/10/2026

## Verified separate slice

- Fresh `327-combined-qa`, from main `42e5e69`, then fast-forwarded to `353d30a`.
  Previous 325 WIP, #324 heads/receipts, earlier overflow/AX instrumentation
  failures and admission refusals were preserved, not rewritten.
- #324 exact `a6a59a3` required checks passed; zero GraphQL review threads and
  CLEAN. Normal source-only squash verified **MERGED `353d30a61adb3c25eb0d863ce74bba8d58cba286`**
  at 19:43 BRT. See `324-exact-head-ci.json` / `324-merge.json` in this packet.
- After the loaded heavy slot refused the first repaired packet at 19:43, the
  slot became inactive at 19:49. One bounded admitted packet then executed the
  repaired full Chromium AX source and separate deployed checks. Both completed.
  Geometry is **1366/1366 and 390/390**; all quota annotations appear as unignored
  StaticText; polling, focus/details, state fixtures and render-network sentinels
  passed. Source receipt: completed/checks_executed=true, findings=[]. Deployed:
  completed=true, four polls, no unexpected network or console errors.
- This verified previous #323/`45b7d0ef` UI source and exact deployed
  `/nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0` UI/CSS
  bytes plus actual Desktop weekly quota producer. **It is not combined-gauge
  source or live acceptance.**

Receipts/DOM/PNGs and command output live under
`docs/evidence/combined-qa-2026-10-07/{legacy-source-ax,legacy-deployed-ax}/`.
No raw operational journal rows, identities, costs, credentials or content copied.

## New contract and executable oracle

One Pi client completion journal only. Add output tokens over the fixed shared
60-second interval; do not add proxy/local-central copies, prompt/cache tokens or
average per-request generation rates. Observed Pi on this host is not all-host or
non-Pi coverage. Workload selection/HTMX must not narrow the headline.

`tests/check_combined_accounting.py` binds directly to pT's named
`output_usage.read_usage` / `refresh` / `cached` seam; it does not use a second
reader implementation as the system under test. Its synthetic journal fixtures
assert 600+1200=30, duration/input/cache exclusion, Codex aliases/direct Desktop,
repeat reads/same-inode alias, declared **[sample-60, sample)** boundaries, idle,
warm-up, missing/negative/nonfinite/future/missing-output, partial write, file
replacement, unchanged cache age, stale projection and no getter I/O.
`tests/test_combined_independent.py` carries the same runnable assertions into CI.

At 19:55 the first bound candidate attempt was refused before execution. At
**19:58**, a bounded admitted job ran the real candidate reader: **16 independent
assertion cases PASS; pytest wrapper 2 passed in 0.06s**. Reader SHA-256
`54668c9fd9277bc66a3bc6a460f3249aa4b726b3ea136f2cc4b8e5860274924b`
was identical before/after. See `candidate-arithmetic.json`, `candidate-pytest.txt`
and `reader-{before,after}.sha256`. It is still a fingerprinted uncommitted 326
candidate, not immutable merged-source or live acceptance. No pT source edits.

Separate `tests/check_combined_browser.py` reuses repaired geometry/AX/capture and
keyboard helpers, asserts combined30 despite sibling120/local proxy9000 outputs,
source picker and HTMX consistency, cache-only/no-network rendering, and idle/
stale/error/warmup/unsupported UI. It has a separate read-only exact-deployed mode.
The candidate browser launch after 20:07 was refused before execution (loaded
shared slot), so no new browser receipt/pass is claimed. Subsequent control
probes for formatter/metadata/delivery were refused with exit74 (small pool
unavailable, 25-second caller deadline, nothing executed). At **20:13 BRT**, the control lane granted final formatter/lint: all three new
checks pass Ruff; final deployed-receipt enhancement is linted. Normal new-slice
hooks/PR/current-head CI and final-source/live combined acceptance remain pending,
not green. At that checkpoint no combined source PR existed, and the heavy slot
was active since 20:13:29.

The normal 20:14 commit hook executed and correctly rejected two new test-helper
clones (129 and 51 tokens). QA refactored the shared cause: extracted the existing
isolated render app/network guards into `check_gauge_wire.render_app`, reused by
legacy and combined checks; no suppression or bypass. Hook re-verification is
pending, not an asserted fix. Clone diagnostic is durable in this packet. The subsequent final lint/restage/normal-hook/push command was
admission-refused (exit74, nothing executed); commit/PR delivery is not asserted.

## Independent observational reference (not acceptance)

A bounded 2 MiB read at **19:49:07 BRT** observed 92 completion events and 89,813
output tokens /60 = **1,496.8833 accounted output tokens/s**. Six provider aliases
were observed, with zero invalid tail rows, complete trailing line and >60s tail
coverage. See `live-journal-reference.json`. This is an aggregate checkpoint only,
not a constant speed, the pT reader, streamed counts, synthetic inference, or proof
of the live UI's combined semantics.

## Pending runtime and reversal

pV alone activates the exact accepted immutable combined build. QA must bind the
new source/build hashes, persistent/effective unit and served CSS/DOM, then run
read-only admitted browser checks; no broad deployment/auth/device changes.
Current `95ll5i712…` acceptance remains old selected-speed UI. No final all-green
claim. QA receipts/test delta is reversible by one normal `git revert` PR; no
operational state was mutated. Canonical coordination updated in UI-TEAM.
