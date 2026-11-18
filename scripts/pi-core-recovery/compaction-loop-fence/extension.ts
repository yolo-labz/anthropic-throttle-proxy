// Offline candidate. Loading registers explicit scoped commands; it installs no hooks.
import { lstatSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { qualify } from '../core-upgrade-guard.ts';
import { qualifiedRuntime, installFence } from './fence.mjs';

export default function (pi) {
  for (const action of ['enable', 'disable']) pi.registerCommand(`compaction-loop-fence-${action}`, {
    description: 'Enable reviewed identical-input automatic compaction retry fence for this idle session only',
    handler: async (nonce, ctx) => {
      if (!/^[a-f0-9]{64}$/.test(nonce)) throw Error('invalid nonce');
      const directory = join(homedir(), '.local/state/throttler-recovery-20261111/core-upgrades');
      const parent = lstatSync(directory);
      if (!parent.isDirectory() || parent.uid !== process.getuid() || (parent.mode & 0o777) !== 0o700) throw Error('unsafe request directory');
      const path = join(directory, `${nonce}.request.json`);
      const stat = lstatSync(path);
      if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o600) throw Error('unsafe request');
      const request = JSON.parse(readFileSync(path, 'utf8'));
      if (request.nonce !== nonce) throw Error('nonce mismatch');
      const runtime = await qualifiedRuntime(process.env.PI_FLEET_INSTALL);
      // Reobserve AFTER async imports; no await follows attestation before enable.
      const actual = { pid: process.pid, session: ctx.sessionManager.getSessionFile(), cwd: ctx.cwd,
        provider: ctx.model?.provider, model: ctx.model?.id, effort: pi.getThinkingLevel(),
        install: process.env.PI_FLEET_INSTALL, role: process.env.PI_ROLE_RESOLVED,
        idle: ctx.isIdle(), pending: ctx.hasPendingMessages(), editor: ctx.hasUI ? ctx.ui.getEditorText() : null };
      qualify(request, actual);
      const controller = installFence(runtime);
      controller[action](ctx.sessionManager);
      const receipt = join(directory, `${nonce}.compaction-fence.json`);
      const staging = `${receipt}.${randomBytes(16).toString('hex')}`;
      writeFileSync(staging, JSON.stringify({ nonce, pid: process.pid, version: controller.version,
        fingerprint: runtime.fingerprint, enabled: action === 'enable', inference: false }) + '\n', { mode: 0o600, flag: 'wx' });
      renameSync(staging, receipt);
    },
  });
  const directory = join(homedir(), '.local/state/throttler-recovery-20261111/core-upgrades');
  const parent = lstatSync(directory);
  if (!parent.isDirectory() || parent.uid !== process.getuid() || (parent.mode & 0o777) !== 0o700) throw Error('unsafe receipt directory');
  const loaded = join(directory, `${process.pid}.compaction-fence-loaded.json`);
  const staging = `${loaded}.${randomBytes(16).toString('hex')}`;
  writeFileSync(staging, JSON.stringify({ pid: process.pid, version: 'prepared-dispatch-fence-v1',
    generation: randomBytes(32).toString('hex') }) + '\n', { mode: 0o600, flag: 'wx' });
  renameSync(staging, loaded);
}
