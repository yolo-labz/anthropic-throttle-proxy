import { createHash, randomBytes } from 'node:crypto';
import { lstatSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { qualify } from '../core-upgrade-guard.ts';
import { observeNative } from '../context-branch-recovery/restore.ts';

export const digest = bytes => createHash('sha256').update(bytes).digest('hex');

export function validateIncident(request, bytes, leaf) {
  if (request.incident !== 'home-w3f-p1-20261111' || request.targetLeaf !== '74edcb64' ||
      leaf !== request.expectedLeaf ||
      digest(bytes) !== request.transcriptSha256 || !Number.isSafeInteger(request.originalBytes) ||
      request.originalBytes <= 0 || request.originalBytes >= bytes.length) throw Error('incident snapshot mismatch');
  const repaired = request.externalParentRepair === true;
  const lines = bytes.toString('utf8').split('\n');
  const prefix = repaired ? Buffer.from(lines.slice(0, request.normalizedPrefixRows).join('\n') + '\n') : bytes.subarray(0, request.originalBytes);
  const reconstructed = repaired ? Buffer.from(lines.slice(0, request.normalizedPrefixRows).map(line => {
    const entry = JSON.parse(line);
    return request.normalizedCompactIds.includes(entry.id) ? JSON.stringify(entry) : line;
  }).join('\n') + '\n') : prefix;
  if (repaired && (!Number.isSafeInteger(request.normalizedPrefixRows) || request.normalizedPrefixRows <= 0 ||
      digest(prefix) !== request.normalizedPrefixSha256)) throw Error('normalized original prefix mismatch');
  if (digest(reconstructed) !== request.originalPrefixSha256 || reconstructed.length !== request.originalBytes || prefix.at(-1) !== 10) throw Error('original prefix mismatch');
  const entries = bytes.toString('utf8').split('\n').filter(Boolean).map(JSON.parse);
  const original = prefix.toString('utf8').split('\n').filter(Boolean).map(JSON.parse);
  const index = new Map();
  for (const entry of entries) {
    if (entry.type === 'session') continue;
    if (!entry.id || index.has(entry.id)) throw Error('ambiguous entry identity');
    index.set(entry.id, entry);
  }
  const suffix = entries.slice(original.length);
  if (suffix.length < 2 || suffix.length > 10 || suffix.at(-1)?.id !== request.expectedLeaf || suffix[0].id !== 'c5582014' || suffix[0].parentId !== (repaired ? null : '1fe7721e') ||
      suffix[1].id !== 'a4bbb629' || suffix[1].parentId !== 'c5582014' || index.has('1fe7721e') ||
      suffix.slice(0, 2).some(entry => entry.type !== 'custom' || entry.customType !== 'pi-artifact-cleanup') ||
      suffix.slice(2).some((entry, i) => entry.parentId !== suffix[i + 1].id ||
        !(entry.type === 'model_change' && entry.provider === 'claude-lane' && entry.modelId === 'claude-opus-5-5' ||
          entry.type === 'thinking_level_change' && entry.thinkingLevel === 'max' || entry.type === 'session_info' ||
          entry.type === 'custom_message' && ['context-lean', 'context-watchdog'].includes(entry.customType))) ||
      original.at(-1)?.id !== request.targetLeaf || index.get(request.targetLeaf)?.type !== 'context_edit') throw Error('not exact dangling cleanup suffix');
  const branch = []; const seen = new Set(); let id = request.targetLeaf;
  while (id !== null) {
    if (seen.has(id) || !index.has(id)) throw Error('invalid original lineage');
    seen.add(id); const entry = index.get(id); branch.push(entry); id = entry.parentId;
  }
  const work = branch.filter(entry => ['message', 'compaction'].includes(entry.type)).map(entry => entry.id);
  if (work.length !== 10 || JSON.stringify(work) !== JSON.stringify(request.originalBranchWorkIds) ||
      !branch.some(entry => entry.type === 'message' && entry.message.role === 'user')) throw Error('original work differs');
  return { branchEntries: branch.length, workEntries: work.length };
}

async function navigateUnchanged(ctx, request, before, observe) {
  const result = await ctx.navigateTree(request.targetLeaf, { summarize: false });
  if (result.cancelled || result.aborted || ctx.sessionManager.getLeafId() !== request.targetLeaf) throw Error('native navigation not proven');
  qualify(request, observe());
  if (!readFileSync(request.session).equals(before)) throw Error('unexpected history mutation');
}

function owned(path, directory = false) {
  const stat = lstatSync(path);
  if (stat.uid !== process.getuid() || (stat.mode & 0o777) !== (directory ? 0o700 : 0o600) ||
      !(directory ? stat.isDirectory() : stat.isFile())) throw Error('unsafe incident path');
}

export default function (pi) {
  const directory = join(homedir(), '.local/state/throttler-recovery-20261111/dangling-tail-recovery-v2');
  pi.registerCommand('dangling-tail-restore', {
    description: 'Restore only the reviewed Horaco incident branch without inference',
    handler: async (nonce, ctx) => {
      if (!/^[a-f0-9]{64}$/.test(nonce)) throw Error('invalid nonce');
      owned(directory, true); const path = join(directory, `${nonce}.request.json`); owned(path);
      const request = JSON.parse(readFileSync(path, 'utf8'));
      if (request.nonce !== nonce || request.provider !== 'claude-lane' || request.model !== 'claude-opus-5-5' ||
          request.effort !== 'max' || request.role !== 'orch') throw Error('incident selection differs');
      const observe = () => observeNative(pi, ctx);
      qualify(request, observe()); const before = readFileSync(request.session);
      const proof = validateIncident(request, before, ctx.sessionManager.getLeafId());
      if (!readFileSync(request.session).equals(before)) throw Error('snapshot changed');
      writeFileSync(join(directory, `${nonce}.intent.json`), JSON.stringify({ nonce, proof,
        transcriptSha256: digest(before), targetLeaf: request.targetLeaf }) + '\n', { mode: 0o600, flag: 'wx' });
      await navigateUnchanged(ctx, request, before, observe);
      pi.appendEntry('pi-dangling-tail-recovery', { nonce, targetLeaf: request.targetLeaf,
        previousLeaf: request.expectedLeaf, originalPrefixSha256: request.originalPrefixSha256 });
      const branch = ctx.sessionManager.getBranch(); const last = branch.at(-1);
      if (last?.customType !== 'pi-dangling-tail-recovery' || last.parentId !== request.targetLeaf ||
          !readFileSync(request.session).subarray(0, before.length).equals(before)) throw Error('durable branch not proven');
      writeFileSync(join(directory, `${nonce}.restored.json`), JSON.stringify({ nonce, pid: process.pid,
        targetLeaf: request.targetLeaf, persistedLeaf: last.id, proof, inference: false }) + '\n', { mode: 0o600, flag: 'wx' });
    },
  });
  owned(directory, true);
  const loaded = join(directory, `${process.pid}.loaded.json`); const staging = `${loaded}.${randomBytes(16).toString('hex')}`;
  writeFileSync(staging, JSON.stringify({ pid: process.pid, version: 'horaco-dangling-v2',
    generation: randomBytes(32).toString('hex') }) + '\n', { mode: 0o600, flag: 'wx' });
  renameSync(staging, loaded);
}
