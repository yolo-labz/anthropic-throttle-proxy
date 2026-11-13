# Detached context branch recovery

Explicit one-command operational repair. No hooks, timers, inference, model changes,
shutdown, or automatic recovery. The extension refuses active streams, compaction,
pending messages, editor drafts, identity drift and changed transcript bytes.

Offline acceptance:

```sh
node --test scripts/pi-core-recovery/context-branch-recovery/test-restore.mjs
node scripts/pi-core-recovery/context-branch-recovery/check-native.mjs \
  ~/.local/share/pi-fleet/1.4.0-84393c6d-r2 \
  ~/.local/share/pi-fleet/1.5.2-83a4dd90
```

The native oracle uses synthetic persistent files, the installed extension loader,
ExtensionRunner, SessionManager and AgentSession.navigateTree. It reopens the result
to prove history and selected model/effort survive restart. No provider is called.

An operator must first review and load `restore.ts` through the established guarded
reload protocol, then create an owned mode-0700 directory at
`~/.local/state/throttler-recovery-20261111/context-branch-recovery`. Write a fresh
mode-0600 `<nonce>.request.json`: existing core guard schema 1, unpredictable
64-hex nonce, expiry within five minutes, exact current PID/session/cwd/provider/
model/effort/install/role; additionally `expectedLeaf`, `targetLeaf` and
`transcriptSha256`. `session` means the exact native JSONL filename.

After the native reload/editor-restored barrier, require a fresh `<pid>.loaded.json`
for this exact PID, version `context-branch-native-v1`, changed unpredictable
generation and current load time. It is written only after command registration.
Missing/stale proof means never submit the slash command. The operator creates
the private receipt directory before discovery or reload.

Submit `/context-branch-restore <nonce>` only after fresh native empty-editor,
idle/no-queue qualification. The current branch must be metadata-only and rooted
at detached `pi-artifact-cleanup`. The target must be the latest prior nonuser
work message, with real human ancestry and identical historical model/effort.

An exclusive intent precedes navigation. Navigation does not summarize. One native
`pi-context-branch-recovery` audit append links to the restored target and makes
the chosen leaf durable; original bytes remain an unchanged prefix. Both native
observations and hashes are recorded. Any intent without a restored receipt is an
uncertain outcome requiring inspection: never blindly resend or replace the nonce.

Reversal requires an independently reviewed idle native tree navigation to the
recorded old leaf, followed by a native audit append there. No raw JSONL rewrite,
history deletion or historical model restoration is permitted. This narrow repair
does not automatically authorize reversal or infer why the null leaf originated.
