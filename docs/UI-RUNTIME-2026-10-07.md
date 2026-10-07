# UI runtime delivery — 07/10/2026

Owner: `thr-ui-runtime`, w1P:pV. Generator: openai / GPT-6.1 Sol.

**Current status — 07/10/2026 19:18 BRT:** corrected protected source #323
(`45b7d0ef`) is activated only on `:8765`. Persisted/effective/imported package,
ten source-file fingerprints and served CSS agree. Automatic producer is fresh;
actual `/ui` reports **91.2% weekly remaining**, not monthly credits/throughput.
Independent complete deployed-browser acceptance remains with pW; no full
browser-green claim. Historical failures/blockers below are retained, not current
runtime status. Nix declaration #2682 remains unmerged on an unprotected base.

## Scope and hypothesis

Desktop `:8100` weekly subscription had no independent gauge producer/wiring;
Token Plan `mimo-desktop` / `:8773` monthly purchased credits cannot substitute.
Falsifiers and acceptance: `specs/318-desktop-quota-report/plan.md` and `tasks.md`.
Impeccable **skill-v4.5.0** explicitly loaded; context command accepted existing
visual system. Detector on the modified reader returned `[]`; no visual redesign.

## Producer/consumer contract

- CLI: run `src/anthropic_throttle_proxy/desktop_report.py` with the installed
  Desktop bridge Python, `--backend` and `--output`. No new dependency, model
  call, credential output or raw exception/log forwarding.
- Out-of-process JSON producer: atomic 0600 schema 1, UTC `generatedAt`,
  `intervalSeconds=300`; exactly one `mimo:desktop-subscription` / `kind=mimo`.
- Desktop `percent` means **remaining**. Weekly `usedPercent=100-percent`,
  `remainingPercent=percent`, `unit=percent`; only measured optional reset epoch.
  No purchased-credit allowance, money, fabricated throughput or burn pace.
- Consumer: `THROTTLE_MIMO_DESKTOP_REPORT`, independent of Token Plan's
  `THROTTLE_MIMO_REPORT`. Cached local file read only. Missing/invalid clocks,
  ambiguity, wrong units or missing file => unknown; two missed intervals =>
  stale, including an exhausted previous sample. Report failure replaces rather
  than retains a previously healthy sample. Last observation is shown in row detail.
- One installed account only; multiple accounts explicitly unknown until a
  per-account contract exists. Existing bridge SSO reused, not reimplemented.
- Initial ConfigManager-based wrong-cwd live read failed unknown and generated
  default files in this feature worktree; only those verified freshly-created
  defaults were removed. Inspection then found ConfigManager also SAVES defaults
  on load errors: the current producer therefore **never imports ConfigManager**.
  It reads the already-encrypted single-account seed with the backend's installed
  Fernet implementation and uses only its existing read-only SSO/usage module.
  Synthetic checks cover no mutating import; real collection at 18:10 returned
  **93.1% remaining**, and private config/key were byte-identical afterward.
  Cwd isolation/log suppression remain tested. Private seed not copied/emitted.

Integration coordination is recorded in canonical `UI-TEAM-2026-10-07.md`.
pN's dirty NixOS-2681 is untouched; separate pin worktree is
`/home/notroot/NixOS-2683-throttle-ui-runtime`.

## Historical pre-delivery evidence (source vs live)

| Evidence | Result |
| --- | --- |
| Source main #316 | Protected `main`, `66ad9dc9df82887bccc90572ae0d84dadf5df5fe` |
| Before reader, synthetic regressions | 12 failed, `specs/318-desktop-quota-report/red.txt` |
| Initial targeted iteration | 59 passed, 3 failures corrected (strict UTC timestamp shape; test fixture directory contains another file); not final acceptance |
| Ruff lint + format checks | Passed on current working tree before PR |
| Real allowlisted producer, 07/10 17:52 BRT | `ok`, **93.3% weekly remaining**, reset 12/10; `live-producer.json` |
| Real producer file | Atomic `0600`, separate from Token Plan |
| Heavy local tests/browser | BLOCKED: heavy admission first unit-contention, then `pressure-high`, cause I/O; no fallback/unbounded local job |
| Live pre-delivery `:8765` health | `inflight=0`, `served=0`, upstream remains closed `http://127.0.0.1:1` |
| Live pre-delivery imported build | `/nix/store/jmak9s5hhv33bp53npypcnlkcrjn2cxz-anthropic-throttle-proxy-0.1.0/lib/python3.14/site-packages/anthropic_throttle_proxy` |
| Effective/persistent overriding ExecStart | Same `jmak9s5...`; base HM unit still older `4dywagmi...`, masked by persistent incident drop-in |
| Live pre-delivery `:8773` | Token Plan upstream, independent process; not restarted |

No browser screenshot, final full-suite green, merged source, build activation or
live gauge acceptance was established by that historical baseline. Source #316
was NOT the baseline imported build. The dirty main NixOS secret file was not touched.

## Delivery plan / rollback

1. Normal hooks and protected source PR, required CI actually green.
2. Exact merged-source/hash pin in isolated NixOS-2683; only build the package,
   not a system closure/switch. Nix `main` protection must be checked; an
   unprotected base forbids autonomous merge.
3. Prepare immutable rooted package and a one-service persistent unit drop-in;
   retain prior drop-in bytes, base-unit chain and rooted old package. Preserve
   closed Anthropic transport/routing/knobs, change only ExecStart + Desktop
   report environment. Do not restart Desktop, Token Plan, Z.AI, central or Herdr.
4. Idle health/queued checks immediately before authorized `:8765` restart.
5. Prove persisted chain, effective ExecStart and imported health build all
   agree; prove served real `/ui` changed and contains a fresh independent
   Desktop row. Browser acceptance remains separate from wire/unit fixtures.
6. Rollback: restore retained previous one-service override, daemon-reload,
   restart only this idle service; verify imported old build/closed upstream.

## Exact-head CI failure and correction

Source PR **#319**, initial head `0add715ab1e59d5e48ada15194c99ada2e8314e2`:
- CI run **37686095391**: Ruff lint/format success, **2,088 passed**, 155 warnings
  in 80.73s. This is remote executable acceptance, not local/browser acceptance.
- Required scan run **37686095470 FAILED**. Scanner log explicitly names the
  same SCM revision; report uploaded 18:02:22 BRT, CE task
  `b31dc286-111f-44f5-ab3e-40d023491307`, historical analysis
  `4a747c36-0dc3-431d-b774-56d5756e54e7` binds that exact revision via
  `project_analyses/search` (persisted JSON receipts in spec directory).
- `qualitygates/project_status?analysisId=4a747c36-0dc3-431d-b774-56d5756e54e7`
  returned **ERROR**, new violations **1**, new coverage 93.5%, new duplication
  0.0%. Thresholds remain 0 / 80% / 3%; no exclusion/threshold change.
- Concrete finding **python:S3358**, issue
  `2415a855-fb74-46b3-a12d-ace8565fc5fe`, `lanes.py:608`: nested conditional
  expression. Extracted status calculation and explicit stale override.
- Shared Community dashboard subsequently analyzed a sibling revision and closed
  the issue because this code was absent. That is **not our fix acceptance**;
  projectKey/current-green metrics remain revision-mismatched/unknown. No rerun
  of the failed head and no deployment through the red gate.
- Retrieval used authenticated read-only tailnet Dokku nginx with the Sonar Host
  header; Cloudflare public data API returned 403. Credentials stayed in memory,
  never command arguments/output. No Actions/#315 change or unrelated restart.

## Verified source landing and scoped runtime blocker — 07/10/2026 18:23 BRT

- Corrected head `b3059ebf7504a567b521403cdbf1a1501c8ba793`: all required
  checks PASS; CI **37687632981**, **2,095 passed**, 155 warnings in 85.91s.
  Required scan **37687632972** names that exact SCM revision and PASS.
- Historical corrected analysis `f7270078-db56-4ec6-a31a-17e8d5d9e7f7` is bound
  to b3059eb by `project_analyses/search`; analysis-id gate is OK, zero new
  violations, 93.6% new coverage, 0.0% duplication. Receipt:
  `specs/318-desktop-quota-report/sonar-corrected-analysis.json`.
- Coordinator normal-squash merged protected #319 at 18:16:29 BRT as
  **1969bdcbf54b5e477ca7362184470becd1ecf5ef**. `gh pr view` confirms MERGED;
  zero review threads. Copilot's quota-only comment is not an approval.
- #320 contrast fix also verified MERGED (`4aedd3fc0ac00f1c5e7a0cb3a3a5593216061231`),
  all actual required checks PASS. Standalone NixOS-2683 targets that combined
  exact protected source for the one-service UI delivery.
- Source rollback: one normal revert PR for `1969bdcb`; current-runtime rollback
  retains exact `90-throttler-7236d96.conf` content in
  `specs/318-desktop-quota-report/rollback-90-throttler.conf`.

**Not deployed.** Heavy package attempts did not start: occupied shared user
slot (18:19/18:21), recovery/reopen-pending (18:20), memory budget refusal
(18:22, MemAvailable 24,836,708 KiB <24 GiB), then I/O pressure refusal despite
memory recovery (18:23, I/O full 16.34%). No local small-lane build/test/browser
fallback, no remote unallocated build, no unit/cgroup limit changes. Local
browser acceptance and final targeted check remain unexecuted; full acceptance
above is remote CI, not a live UI result.

NixOS-2683 holds the exact-source pin with temporary **uncommitted** `lib.fakeHash`
only for the standard fixed-output mismatch procedure. It is not a measured
hash, committed pin, delivered package or usable build. Producer service/timer
and persistent UI override templates are retained there with explicit package
placeholders; none is activated. pN's dirty/source worktree and ten iPad clients
are untouched. Nix main remains unprotected; no autonomous Nix merge, full
system switch, reboot or bridge credential adoption is implied.

Live `:8765` still imports old `jmak9s5...`, has no Desktop row/source picker,
upstream still closed loopback sink, original PID retained. Real dated producer
sample **93.1% weekly remaining** at 18:10 is not continuously fresh: timer is
not installed yet; consumer would correctly stale it after 600s. Scoped
activation still requires an admitted exact package build, rooted rollback,
immediate idle checks, full persisted/effective/imported chain comparison and
actual `/ui`/browser receipt. Central `GIT_REV` read-only receipt:
`e16d15900f73e9f82c1714738368f5db3f21cfd0`, running; no central restart/delivery.

## Resumed scoped producer delivery — 07/10/2026 18:51 BRT

#322 source receipt is confirmed MERGED (`1240058cc53ecb422487b1627eae5bc5dbf0b64f`)
by coordinator. This new runtime receipt lives in isolated
`anthropic-throttle-proxy-324-ui-runtime-delivery`; previous worktrees/evidence
are retained. No shared-main edits or new workers.

- Normal heavy admission built the exact measured `4aedd3fc` candidate at 18:38:
  **8.436s, 181.2 MiB peak**, package
  `/nix/store/czidrgs5ydsjdq16kbm9xas7kxwcdwi8-anthropic-throttle-proxy-0.1.0`;
  Nix import check passed, existing-path GC root retained. This is source/build
  acceptance only. **UI remains inactive**: #321's admitted confirmation
  genuinely failed **473px/390 and 1391px/1366**. pT owns exact root-cause fix
  in fresh `323-ui-scroll-geometry`, pW owns confirmation; no duplicate UI work.
- Related read-only producer service/timer installed from immutable rooted Nix
  files and enabled every five minutes. CPU 50%, 128 MiB, 16 tasks, timeout 60s,
  read-only home, atomic report 0600. No bridge credential/key adoption or client
  catalogue changes. It uses the source-tested candidate producer only.
- First timer-triggered collection **18:41:47→18:41:54**, success/status 0,
  **92.3% weekly remaining**. Second **AUTOMATIC** run
  **18:46:47→18:46:54**, success/status 0 and refreshed independent report;
  next tick 18:51:47. This proves ongoing report freshness, not UI consumption.
- Original `:8765` PID **3842394**, imported old jmak9s5, cancelled closed sink
  intact. Desktop `:8100`, Token Plan, Z.AI and shim PIDs match preflight exactly;
  none restarted, original ten clients untouched.
- Declarative equivalent added only in isolated NixOS-2683's existing UI-only
  `mimo-dashboard.nix`; pN's branch/module untouched. Reuses shared package,
  installed backend Python and original monthly probe unchanged. Existing
  hermetic `assert-mimo-dashboard` final admitted check **PASS**, plus **27
  exact-identity report fixtures PASS**, 11.507s / 691.4 MiB peak. Initial test
  exposed HM ExecStart JSON-list shape; normalized the test without weakening
  assertions. No full system closure build/switch.
- Actual flake-overlay candidate package `mhkm6z1...` also built in that native
  check; its producer bytes match the standalone candidate. Final corrected UI
  will use the canonical flake package, not conflate dependency closures.

Receipts/template/rollback path: `NixOS-2683-throttle-ui-runtime/meta/`:
`2683-before-runtime.json` (whole persisted chain component paths/hash + effective
ExecStart), `2683-package-build-candidate.txt`,
`2683-producer-first-timer-sample.json`, `2683-producer-second-tick.json`,
`2683-desktop-wiring-check-final.txt`, inactive candidate override and rooted unit
paths. systemd verification exited 0; unrelated inherited unit warnings were
not modified or hidden.

Remaining UI activation: pT protected corrected exact head + real pW confirmation,
resolve measured final filtered source hash, admitted canonical package build,
retain old/new roots, immediate idle checks, immutable additive persistent
`99-ui-runtime-2026-10-07.conf`, restart ONLY :8765, compare full
persisted/effective/imported chain, fresh `/ui` row/source picker and browser seam.
No acceptance is inferred from known failing geometry. Nix main/unprotected
source and bridge credential adoption remain separate gates.

Producer rollback (if this new related timer is wrong): disable/stop only
`mimo-desktop-quota-report.timer`, preserve its last timestamp so the consumer
ages it stale; remove only this newly-created service/timer symlink and reload.
Never rotate credentials, restart the bridge or alter ten clients to undo this
read-only probe. UI override has not been installed, so UI rollback is presently
unnecessary at 18:51; retained old drop-in remains exact.

## Final-source scoped activation — 07/10/2026 19:18 BRT

- Protected **#323 MERGED** as `45b7d0ef3321cca9c6d4e8513686efa30d889c1a`;
  required checks passed for exact head `4862145d`. pT's causal correction binds
  clipped quota annotations to the scrollport. Its branch browser/tests pass is
  distinct from pW's independent deployed oracle.
- Final filtered hash **sha256-wpx/L5yiv2vo9rNndRIcvur58MYp7Rkke4LivnWXJrY=**;
  intentional mismatch preserved in `meta/2683-final-source-hash.txt`.
  Normal heavy, one-job/one-core canonical desktop-flake package build and native
  wiring check passed together: **27.799s, 695.3 MiB peak**, import check PASS,
  native wiring/27 report fixtures PASS. Exact package:
  `/nix/store/95ll5i712vsrzri85hsv675ljjr09kk2-anthropic-throttle-proxy-0.1.0`.
- Immediate preflight: zero inflight and six zero bearer queues; inherited unit
  hashes/PIDs still matched. Added immutable rooted persistent
  `99-ui-runtime-2026-10-07.conf`, updated only related producer's package path,
  restarted **only** authorized `anthropic-throttle-proxy.service`.
  Effective start **19:07:13**, PID **1096484**. Closed Anthropic upstream
  `http://127.0.0.1:1`, central empty; all existing drop-ins retained byte-identical.
- **19:18 executable runtime receipt PASS:** complete persisted/effective chain,
  actual imported build, native `/proc` command-line observation, ten deployed
  source fingerprints versus squash45b7d0ef, and HTTP-served CSS all agree.
  Separate monthly/weekly report environment is preserved. Actual `/ui` has the
  independent Desktop row and **91.2% weekly remaining**, matching the producer
  sample generated19:17:12, age77.3s, mode0600. Timer is enabled/active, five-minute
  real samples continue. Old/new packages and immutable units/override have
  retained GC roots. Nothing was inferred from package basename alone.
- Full runtime check: `python ~/NixOS-2683-throttle-ui-runtime/meta/2683-verify-runtime.py`.
  Its allowlisted JSON/output is copied to `docs/evidence/ui-runtime-2026-10-07/`.
  No raw credentials/config, account DOM or monetary payload retained.

### Important verification correction

The19:09 aggregate assertion failed because the **Desktop bridge PID changed**,
not because HTML/imported build failed. Named checks19:12 passed the UI/build,
report freshness and inherited chain. Token Plan/Z.AI/shim still match preflight.
Bridge1718616→1181854: journal explicitly records **Scheduled restart job,
counter1** and Started at **19:08:30**, after the UI start. Bridge is active/
running, Result success. Runtime seat issued no bridge restart, credential/key
adoption or client mutation. The exit cause is **not established**; no attribution
to this UI/probe, pN or credential adoption is made. This discontinuity is retained
in `2683-desktop-pid-change.json`, not hidden behind an all-PIDs-unchanged claim.

The initial complete-receipt run then hit a `/proc` **instrumentation namespace**
error: the admitted control unit cannot see host PID1096484. Native metadata read
verified the living process's exact wrapped command line; receipt/script compare
that observed PID against current systemd PID instead. Failed output retained as
`2683-runtime-verification-proc-namespace-failure.txt`; no admission fence changed.
Several control admissions were refused before execution; no fallback. These are
receipt failures/history, not evidence that an app restart or browser pass occurred.

### Reversal and remaining boundary

One-command scoped UI reversal:
`bash ~/NixOS-2683-throttle-ui-runtime/meta/2683-ui-runtime-rollback.sh`.
It requires exact installed override identity and immediate idle/closed-upstream
checks, removes **only** this additive99 symlink, reloads/restarts only:8765, then
verifies imported rooted oldjmak9s5. `--check` runs non-mutating preflight;
actual reversal has not been executed. Existing incident/closed-sink drop-ins,
producer timer and all other services remain untouched.

Nix source **#2682** carries the declarative counterpart/final shared pin; base
is unprotected and pin can affect future fleet packages. **No autonomous Nix
merge, broad activation or deployment elsewhere.** Reconcile only this slice's
manual reporting links/99 override when an eventual declaration activation is
explicitly authorized; do not substitute a system switch for this scoped receipt.

pW's actual deployed-browser oracle remains separately recorded in
`docs/UI-BROWSER-QA-2026-10-07.md` and canonical UI-TEAM. Its partial measured
1366/1366 and390/390 capture is encouraging but **not full acceptance** while AX
instrumentation repair/admitted final run is pending. No duplicate browser work,
new agents, paid/provider fallback, client change or global GC/sweep.
