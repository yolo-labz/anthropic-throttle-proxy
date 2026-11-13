# Known Horaco dangling-tail incident — reviewed activation still required

This is an explicit repair candidate for **home:w3F:p1 only**, after the original
native process exited and its captured shell remained. It does not relax the
shared recovery guard. It never rewrites a transcript, summarizes, changes a
model, automatically restores a branch, or sends a task prompt.

`launch.py` defaults to planning only. An operator first reads the live shell
screen and verifies that no human command is being edited, then supplies the
SHA256 of the exact `herdr --session home pane read w3F:p1 --source visible
--lines 20 --format text` output as `--reviewed-shell-screen-sha256`. Planning
requires the original PID gone, original shell/CWD unchanged, captured native
state/ready/bootstrap proofs, exact original prefix and ten work IDs, exact
two-entry dangling cleanup suffix, and immutable accepted 1.5.2/source pin.

After independent review, `--execute` repeats these checks and writes an exclusive
launch intent before one native `pane run`. A timeout/nonzero result is uncertain:
inspect the actual pane and intent; never resend. The command replays captured
bootstrap/environment/session and Opus/max, adding only this incident extension.
It does not invoke the shared `recover_core.restart` path or bypass its guard.

After launch, verify actual PID/install/name/role/session, fresh module-specific
`<pid>.loaded.json` generation, native idle/no-pending and empty editor. Prepare
one fresh owned 0600 `<nonce>.request.json` in the owned 0700 incident directory,
using core guard schema 1 with exact current identity and role `orch`, plus the
launch plan's `incident`, `originalBytes`, `originalPrefixSha256`, ten
`originalBranchWorkIds`, and target `74edcb64`. Bind `transcriptSha256` to the
complete current bytes and `expectedLeaf` to the actual current native/disk leaf.
Only the exact two cleanup nodes followed by at most eight same-selection
model/thinking/session-info or exact context-lean/context-watchdog banner entries are accepted; all work or unknown suffixes
hold. These bounded startup entries are needed because native CLI selection may
append the already-selected model/effort while opening a branch without history
selection metadata.

Submit `/dangling-tail-restore NONCE` once. The command validates the original
context-edit leaf and all ten captured work IDs, records an exclusive intent,
uses actual `navigateTree(..., {summarize:false})`, and appends one native audit
entry. Verify the restored receipt, original raw prefix and work ancestry. Any
failure after intent requires inspection, never resend.

Offline native oracle, using only synthetic persistent fixtures:

```sh
node scripts/pi-core-recovery/dangling-tail-recovery/check-native.mjs \
  ~/.local/share/pi-fleet/1.4.0-84393c6d-r2 \
  ~/.local/share/pi-fleet/1.5.2-83a4dd90
```

## Proven external repair variant (v2)

The original v1 plan remains invalid and unchanged. The scheduled history pruner
rewrote Horaco at 19:24 on 11/11/2026: its missing backup converted the dangling
cleanup parent to null and normalized JSON serialization. In-memory reconstruction
proved the exact original 7064708-byte prefix hash, including every original record
value and parent. No transcript bytes were restored or rewritten. The operator has
retired that writer; external transcript replacement while Pi owns its session can
desynchronize the native in-memory branch from disk.

The new launcher requires `--external-parent-repair`, the exact observed full hash,
and the exact reconstructed original prefix. It writes its separate write-once plan
under `dangling-tail-recovery-v2`; the extension reads requests only there and emits
`horaco-dangling-v2` registration proof. Native navigation appends an audit entry
while preserving all current normalized bytes. Startup context-lean/context-watchdog
banners remain allowed; unknown banners, work, selection changes, or external drift
fail closed. Source review, protected acceptance, and operational receipts remain
separate; no live launch is performed by these tests.

After launch, native Herdr registration is a separate metadata-only step. First
prove the new PID and native STATE retain the immutable install, session, cwd,
Opus/max, orch role and bootstrap. Then run exactly once `herdr --session home
agent rename w3F:p1 horaco-orch` and verify native agent get reports that name,
original session/cwd and same PID. The launcher does not claim environment flags
prove native name registration and does not perform this step automatically.
