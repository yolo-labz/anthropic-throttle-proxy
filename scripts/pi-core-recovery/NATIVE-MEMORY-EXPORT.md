# Preserve a live native memory branch

`export_memory_context.py` preserves evidence when a running Pi retains a branch
that differs from its JSONL file. It exports that live branch to a **new private
artifact**. It does not reconcile history, import the artifact, switch sessions,
restart Pi, or resume a task. The history-settlement hold remains in force.

Use only a reviewed controller and an already loaded, qualified guard whose
`core-upgrade-state` command reports native context without changing it. Supply
the existing before receipt and accepted context-mismatch refusal for the exact
seat and PID. Do not reload a working seat to obtain this prerequisite.

```sh
python3 scripts/pi-core-recovery/export_memory_context.py \
  --before /absolute/private/before.json \
  --refusal /absolute/private/nonce.refused.json \
  --directory /absolute/private/export-directory
```

The output directory must have mode `0700`, belong to the current user, and have
no symlink ancestors or whitespace in its absolute path. Its parent must already
exist. The controller creates a random artifact name; never pre-create it.

The controller acquires the existing pane lock and persists one intent before
sending anything. It verifies process/bootstrap identity and an empty editor,
then sends one fresh `/core-upgrade-state nonce`. Only an exact native mismatch
receipt proving the expected pin, idle state, empty queue/editor, unchanged disk
hash and unchanged native branch permits one builtin `/export …jsonl` command.
Neither command requests inference. A truncated footer is not selection proof.

A directory watcher is registered before the commands. Only the exact artifact's
`IN_CLOSE_WRITE` event permits reading it; delivery exit status, an existing valid
JSONL prefix, and wrapped UI status text do not prove completion. Missing proof,
an event overflow, or changed identity/context produces a hold without resending.
Completion waits only within the remaining 90-second operation deadline; native
command delivery can finish before the queued export actually writes its file.
The canonical seat/PID intent prevents another directory from bypassing this rule.

Successful verification records the private snapshot's SHA-256, raw entry IDs and
types, linear exported parents, session header, branch/work digests, selected pin,
and byte-identical original transcript. Artifact permissions become `0600`.
The artifact contains full conversation data; handle it as private history, not
as metadata suitable for logs or a public issue.

The native digest proves branch membership, not every in-memory content byte.
Shared disk work is compared field-for-field except `parentId`, which an external
writer may have changed. Missing/conflicting disk work is reported explicitly.
Pre-existing dangling disk ancestry is recorded without discarding the complete
memory snapshot. These checks do not establish a reconciled union or authorize
using the artifact as a replacement session.

Inspect the durable intent/result and retain any artifact after an uncertain
outcome. Do not repeat STATE/export, delete the intent, or resume work merely
because the export succeeded. Subsequent settlement needs a separately reviewed
plan and authorization.

Offline acceptance:

```sh
python3 -m pytest -q scripts/pi-core-recovery/test_export_memory_context.py
node scripts/pi-core-recovery/check-native-memory-export.mjs /absolute/pi-install
```

The native oracle uses synthetic sessions and the installed builtin exporter. It
makes no provider request and must never receive a live transcript as a fixture.
