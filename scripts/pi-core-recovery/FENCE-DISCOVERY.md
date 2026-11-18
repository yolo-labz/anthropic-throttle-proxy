# Fence discovery on Pi 1.4 and 1.5

The Pi 1.4 native extension loader resolves relative imports from a global file
symlink's lexical location. A global `compaction-loop-fence.ts` symlink directly
to the reviewed `extension.ts` therefore fails to import
`../core-upgrade-guard.ts`. Loading the real target path directly passes and does
not exercise this deployment boundary. Pi 1.5 accepts both paths.

Use a small discovery entry that imports the reviewed real module by absolute
path. Keep implementation files unchanged. Generate its bytes from the approved
path using JSON quoting, rather than interpolating shell text:

```python
entry = 'export { default } from ' + json.dumps(str(reviewed_real_path)) + ';\n'
```

The discovery entry can be a regular file or a symlink to that entry in a private
content-addressed directory. It must import the real implementation, not a copy
placed at the global path. Before replacing an owned discovery pointer, compare
its exact old target and implementation hashes under the coordinator lock. Hold
on drift. Record old and new hashes; replace only that pointer atomically.

Run the actual discovery oracle in separate processes for both installed cores:

```sh
node scripts/pi-core-recovery/check-fence-discovery.mjs "$OLD_PI_INSTALL"
node scripts/pi-core-recovery/check-fence-discovery.mjs "$NEW_PI_INSTALL"
```

The oracle creates a synthetic HOME containing only this extension, reproduces
the original symlink failure on 1.4, and requires discovery through a symlinked
absolute forwarder to register both commands and write its real private loaded
receipt. Direct loading happens afterward, avoiding a warmed direct-import
acceptance result. It opens no session manager and makes no provider calls.

A changed discovery entry is a material correction, not permission to resend a
failed controller. Preserve the original intent and inspect its outcome first.
A separately authorized, fresh continuation must bind the same live PID,
session, cwd, name, role and selection, a valid unchanged original history
prefix and branch, and an empty editor. Reload once and require fresh native
load generations before sending any novel slash command. A rendered toast is
not acceptance. Require fresh native STATE with no pending messages before
fence enable. Missing receipts or uncertain delivery stop; never resend by
elapsed time. Fence-only enrollment has no target leaf, quit, restart, model
selection, work prompt, admission quiesce or pacing action.

`prepare_fence_manifest.py` produces a read-only **provisional** identity manifest
using the frozen reviewed controller's identity/screen helpers. It excludes
known history holds, changed processes, unknown selections, nonempty editors,
compaction frames and ambiguous current branches. Its records explicitly defer
pending-message proof to fresh native STATE. A manifest alone authorizes no
input or fleet rollout.
