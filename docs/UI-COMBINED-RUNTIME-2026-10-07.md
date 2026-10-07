# Combined throughput runtime preflight — 07/10/2026

Owner: thr-ui-runtime / pV. Source implementation remains solely pT's
`326-combined-throughput`; independent arithmetic/browser QA remains pW's
`327-combined-qa`. Isolated runtime receipt worktree: `328-combined-runtime`.
No implementation, client prompt, inference, credential or service change.

## Immutable contract and hypothesis

Canonical contract: Notes-2363 / `1. Projects/Throttler/COMBINED-THROUGHPUT-2026-10-07.md`.
Use only `~/.local/state/pi-harness/usage.jsonl`: generated output tokens divided
by one common wall-clock trailing interval, not request-speed averages, input/
cache tokens, quota or sums of client/proxy/local/central copies. Label Pi/local
completion-accounting scope; other hosts/non-Pi coverage remains unverified.
Workload selection must not narrow the combined headline. Missing/unverified/
malformed/stale/partial coverage cannot become fabricated zero or green. A truly
observed idle interval is zero. Refresh/parsing belongs in bounded background
collection; HTMX render is cache-only.

**Runtime hypothesis:** the current UI service can read the existing journal
without changing credentials or weakening its sandbox. Falsifiers: different
service UID, inaccessible/remapped journal path, unreadable parent/file, or the
accepted background sampler failing to observe the intended authoritative file.

## Deterministic read-only preflight

Verified host clock07/10/2026 **19:43:02**, **19:43:50**, **19:46:00** and
**19:48:55 BRT**. Targeted ledger scan found no combined-throughput implementation
row; supplied canonical contract is the authority, not a re-derived feature.

- Current :8765 PID **1096484**, imported rooted package **95ll5i7…**, protected
  source45b7d0ef. Anthropic upstream remains closed127.0.0.1:1; central empty.
- Native process metadata: real/effective/saved/fs UID1000, GID100. Root mount is
  read-only but readable. Full mountinfo has no home/pi-harness inaccessible
  overlay; only existing proxy state is a writable home bind. Effective
  ProtectHome=read-only, no RootDirectory/RootImage/InaccessiblePaths/bind remap,
  DynamicUser=no. Journal owner1000/mode0644, parent traversal and same-UID open
  PASS. **Filesystem read prerequisites are verified**; actual accepted sampler
  and cache/UI observations remain deployment acceptance, not a claim that old
  #323 already implements combined throughput. No chmod/ACL/secret change.
- One bounded2MiB tail of49,832,124-byte journal: zero parse/invalid rows,
  no partial final line, same device/inode, covers the full observed120s interval.
  At **19:46:02 BRT**,182 events/120s; providers: openai-codex105, codex-b32,
  codex-c5, codex-a7, zai3, direct mimo-desktop-subscription30.
- That checkpoint's60s window:92 events, **68,135 reported output tokens /
  60 =1,135.5833 accounted tokens/s**. This supersedes neither the coordinator's
  earlier1,611.2 checkpoint nor the future dial: rates vary with window time.
  No raw rows, seats, model names, costs, prompt/cache/user content or credentials
  leave the journal. Unsupported non-Pi/other-host coverage is not asserted.
- All sibling PIDs match the preceding runtime receipt: Desktop bridge1181854,
  Token Plan3873577, Z.AI3842786, shim1980. Current UI1096484 is unchanged.
  Capture/recompare these immediately before any future mutation.
- Existing quota producer/timer links, their byte hashes, distinct report paths,
  fresh0600 weekly report and active/enabled timer are preserved. Current and old
  package roots plus immutable99/producer roots are present. Existing rollback
  `2683-ui-runtime-rollback.sh --check` PASS, reversal not executed.

Evidence: `docs/evidence/combined-runtime-2026-10-07/`: allowlisted
`runtime-preflight.json`, `native-process-visibility.json`,
`preservation-check.json`, `retained-rollback-check.txt`, and bounded control
metadata. Copies belong to NixOS-2683 `meta/2683-combined-*`; no raw journal copy.

## Source gate at last verification

At19:48:55, pT's worktree is still at base42e5e69; no combined implementation PR
exists (only unrelated Actions315 is open). **Accepted exact source/CI is NOT
READY. No build, pin update, daemon reload or restart occurred.** Do not poll or
build speculative source. This deterministic preflight is the completed slice;
pV retains the next scoped operation when pT supplies the accepted head.

## Exact next-operation handoff

1. Bind pT's actual PR/head and protected accepted squash. Require that exact
   head's normal required checks/executable acceptance, not an older/sibling
   project dashboard. Read the implementation's real journal option/cache and
   stale/unknown/partial contract; do not invent an environment variable.
2. In owned NixOS-2683, update only the package pin to that accepted revision.
   Measure its filtered fixed-output hash (existing postFetch removes specs),
   retain the intentional mismatch, replace the temporary placeholder, then use
   normal **heavy admission**, native `--max-jobs 1 --cores 1` for the canonical
   desktop flake package. Never build/test/render in the control lane.
3. Retain current95ll5i7, olderjmak9s5, immutable quota units and current99 target
   `/nix/store/fkabv7iwwlvv16iqdyrpgs6spz6mv6fn-99-ui-runtime-2026-10-07.conf`.
   Preserve Nix #2682's unprotected-base merge gate; no broad system switch or
   other-host package activation. Keep monthly/weekly quota configuration intact.
4. Capture whole persisted/effective chain, exact source/hash, journal identity/
   read scope, fresh quota timer and **all** baseline PIDs. Immediately require
   zero model inflight and all bearer queues idle before touching :8765. A busy
   lane is a gate, not permission to send a client prompt or poll inside a job.
5. Use one additive immutable persistent **99z-combined-throughput-2026-10-07.conf**
   after the current99 file, with accepted package ExecStart and only the actual
   source's necessary journal option. Numeric100 would sort BEFORE99, so must
   not be used as an assumed higher-precedence override. Root old/new artifacts.
   Reload/restart **only authorized anthropic-throttle-proxy.service**. No quota
   timer, Desktop/device/bridge/Token Plan/Z.AI/shim restart or secret change.
6. Verify resolved persisted/effective/imported/cmdline/source/CSS agree. Prove
   background sampler reads this journal and actual cached `/ui`/HTMX show the
   combined completion-window/partial-coverage label. Compare against the
   sampler's SAME published window timestamp, not a different live checkpoint.
   Verify workload selection cannot narrow the headline; stale/unknown/idle
   semantics and no render-time journal/network parsing via pT/pW acceptance.
   Preserve quota freshness and every sibling PID. pW owns admitted browser QA;
   do not duplicate source/render work or equate CI with live acceptance.
7. Combined-only reversal removes **only the new99z delta**, with exact-link and
   immediate idle checks; daemon reload/restart only:8765, then verify current
   **95ll5i7** and existing weekly quota row/timer. Leave current99 and the older
   UI rollback unchanged. Record the exact installed target/command after build;
   there is no executable new override to reverse yet.

This preflight is not combined-dial implementation, source merge, new package
or deployment/browser acceptance. No estimate/synthetic inference, paid/provider
fallback, shared-state sweep, global GC or in-job readiness polling.
