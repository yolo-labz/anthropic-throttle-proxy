# Offline temporary native retry fence candidate

Not activated, published, or accepted. Loading `extension.ts` registers scoped enable/disable commands including
`/compaction-loop-fence-enable NONCE`; it installs no hooks or timers. The command
requires an owned mode-0600 short-lived native identity request in the existing
mode-0700 core-upgrades directory, fresh native idle/no-pending/empty-editor state,
and exact fingerprints of reviewed installed 1.4.0/1.5.2 modules. It enrolls only
that native session manager. No changes to installed files, manifests, selected
provider/model/effort, abort behavior, or request signals.

The wrapper saves all three original methods and uses a versioned symbol and
per-session WeakMap. It calls native preparation/serialization/compact functions.
Only a failed default compaction whose supplied stream function was invoked stores
the original prepared-input hash. This is attempted dispatch, not proof of remote
egress or billing. Auth failures and extension cancellation do not store a failure.
The local result observer rejects provider `stopReason: aborted` before native
compaction can append an empty checkpoint; it never mutates the original stream,
response or abort signal. Both fingerprinted summarizers consume only `.result()`.
Missing stream observation preserves the original native fallback without caching.
Unchanged automatic preparation returns false before starting another compaction;
explicit manual compaction bypasses that automatic gate. Successful native
compaction clears the remembered failure. Actual summary text/model/effort/focus
changes are fresh input; usage and metadata-only leaf changes are not.

The key mirrors the durable source candidate: session id, selected model/effort,
custom instructions, native serialized LLM messages/prefix, previous summary and
preparation settings. It is captured before async auth and retained only in memory.
No payload, credential, or raw failure text is persisted.

Offline acceptance (substitute either reviewed installed root):

```sh
node scripts/pi-core-recovery/check-native-compaction-loop.mjs INSTALL exposed
node scripts/pi-core-recovery/compaction-loop-fence/check-native-fence.mjs INSTALL
node scripts/pi-core-recovery/compaction-loop-fence/check-command.mjs INSTALL
```

The real original native class dispatches three identical failed summaries; the
enabled class dispatches once. Near misses cover auth failure, cancellation,
metadata-only leaves, content/effort changes, async identity races, manual bypass,
unenrolled sessions, unknown fingerprints and unsafe explicit-command states.
An actual aborted-result case proves no checkpoint is appended, identical automatic
attempts are suppressed, and an explicit successful native manual recovery works.
The command check uses the actual installed extension loader and runner.

This temporary candidate only fences automatic compaction repeats. It does not
implement the durable source candidate's additional pre-assistant-request overflow
guard, and it does not qualify native settlement or make a busy pane safe to reload.
Active compaction always remains a hold, even if Herdr reports `done`.

Reversal: `/compaction-loop-fence-disable NONCE` uses the same fresh idle, empty-editor,
no-pending native identity attestation. It removes only this session manager from
the in-memory fence; other enrolled sessions and original prototype methods remain intact.

Before invoking either command, verify this module's fresh `<pid>.compaction-fence-loaded.json`
receipt (PID, version and random generation) in the owned 0700 core-upgrades directory.
The atomic receipt is 0600 and follows command registration; it grants no enrollment.
