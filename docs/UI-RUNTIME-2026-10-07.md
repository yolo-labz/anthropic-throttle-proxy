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
- Installed bridge resolves its private seed relative to cwd: producer changes
  cwd only within the isolated collector. Initial wrong-cwd live read failed
  unknown and generated default files in this feature worktree; only these
  verified freshly-created defaults were removed. Private seed not copied or
  emitted. Regression checks cover cwd isolation/log suppression.

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

## Current blockers

Full tests/browser/package build require admitted compute (I/O pressure refusal
at 17:48). CI is the normal remote acceptance path. Scoped activation waits for
exact-head CI/build/idle proofs. Central delivery is not this one-service slice;
its revision must be recorded without restarting it. Durable producer timer is
coordinated with pN; one successful probe is not ongoing collection/persistence.
