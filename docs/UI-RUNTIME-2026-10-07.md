# UI runtime delivery — 07/10/2026

Owner: `thr-ui-runtime`, w1P:pV. Generator: openai / GPT-6.1 Sol.

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

## Evidence (source vs live)

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
live gauge acceptance is claimed by the above. Source current #316 is NOT the
imported build. The dirty main NixOS secret file was not touched.

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

New artifacts after #319 landing are maintained in isolated
`anthropic-throttle-proxy-322-ui-runtime-receipts`, not by editing shared main.
