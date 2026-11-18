# Live Pi history ownership and retired external writers

Verified on desktop, 11/11/2026. This runbook records an operational retirement;
it does not activate services or rewrite transcripts.

A live Pi process owns its session graph: its current leaf and ancestor index
remain in memory while native entries append to the JSONL. Replacing that file
under the process can remove a parent that Pi still knows. Its next successful
append can then reference an ID absent from disk. Changing that unknown parent
to `null` creates a detached root and can leave the current projection containing
only cleanup or other metadata, despite retained work elsewhere in the file.

## Confirmed unsafe maintenance path

The legacy `pi-retry-prune.timer` scheduled `pi-retry-prune.service` every 15 minutes.
The service invoked:

- `pi-purge-retries --apply --quiet`: removed failed-attempt entries, rewired their
  children, and replaced transcript files.
- `pi-repair-parents --apply --backup /var/tmp/none`: rewrote dangling parents using
  backup ancestry; when that ancestry was unavailable, the lookup exhausted to
  `None` and assigned a null parent, then replaced the JSONL.

Neither writer coordinated with live Pi ownership through an active-process guard
or shared lock. File replacement was atomic, but it did not reconcile the running
process's in-memory graph. The missing-parent → null-parent mutation observed in
the Horaco incident matches this code path. Actor attribution for every historical
mutation remains incomplete; this is not proof that the writer caused all 75
high-burn sessions or every failed compaction.

Old Pi 1.4 also advanced its in-memory leaf before persistence without rollback.
An offline native-class failure fixture demonstrates a second way to create a
phantom parent. Accepted Pi 1.5 rolls that failed append back. This separate fix
does not make external live-history replacement safe.

## Effective retirement

The timer is disabled and inactive. The persistent user service drop-in
`~/.config/systemd/user/pi-retry-prune.service.d/90-read-only-history.conf` clears
both inherited `ExecStart` entries and permits only:

```ini
[Service]
ExecStart=
ExecStart=%h/.local/bin/pi-purge-retries --quiet
```

Without `--apply`, that tool is read-only. The retirement did not restart Pi,
interrupt active requests, or change the gateway. Its private receipt is
`~/.local/state/throttler-recovery-20261111/history-pruner-retirement/retirement.json`.
The original unit remains visible beneath the override; inspect effective
`ExecStart`, not merely the inherited service file.

## Read-only verification

These commands inspect service metadata without invoking either history writer:

```sh
systemctl --user is-enabled pi-retry-prune.timer
systemctl --user show pi-retry-prune.timer -p ActiveState -p SubState -p UnitFileState
systemctl --user show pi-retry-prune.service -p ActiveState -p SubState -p ExecStart
systemctl --user cat pi-retry-prune.service
```

Expected: timer `disabled` and `inactive`; effective service command contains
`pi-purge-retries --quiet`, no `--apply`, and no `pi-repair-parents` command.
`is-enabled` returning nonzero for a disabled timer is expected.

For an already captured recovery cohort, `audit_context.py` checks the original
prefix hash and reachable work IDs without opening a persistent native manager:

```sh
python3 scripts/pi-core-recovery/audit_context.py \
  --baseline "$BASELINE_JSON" --manifest "$IDENTITY_MANIFEST_JSON" \
  --targets "$REVIEWED_TARGETS_JSON"
```

Use the exact private baseline, identity manifest and reviewed target records for
that cohort. A dangling parent, changed prefix, missing original work, or absent
restoration proof is a hold, not permission to repair the file externally.

## Recovery and reversal conditions

Preserve the raw history and evidence. At a freshly verified idle boundary, use
reviewed native context navigation to an explicitly identified retained work leaf,
with same-session/model/effort identity checks and a native audit receipt. A
session-scoped failed-compaction retry fence prevents repeated dispatch of the
same failed preparation; it does not repair transcript ancestry. Healthy
compaction and an explicit manual retry remain available.

Never re-enable the mutating writer while any affected Pi process is live. Removing
the drop-in or enabling the old timer restores unsafe behavior and is not an
ordinary rollback. A future replacement requires a reviewed ownership protocol,
verified preservation of ancestry and work IDs, independent backups, and a
regression that rejects concurrent live ownership before any write. A maintenance
window must be explicitly authorized; this runbook does not authorize stopping or
quiescing running work. Keep the read-only retirement until that replacement has
passed its protected source gates and its operational prerequisites are proven.
