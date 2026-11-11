import { createHash, randomBytes } from 'node:crypto';
import { lstatSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { qualify } from '../core-upgrade-guard.ts';

export const digest = bytes => createHash('sha256').update(bytes).digest('hex');

function privatePath(path, directory = false) {
  const stat = lstatSync(path);
  if (stat.uid !== process.getuid() || (stat.mode & 0o777) !== (directory ? 0o700 : 0o600) ||
      !(directory ? stat.isDirectory() : stat.isFile())) throw Error('unsafe owned path');
}

function receipt(directory, nonce, phase, data, exclusive = false) {
  const file = join(directory, `${nonce}.${phase}.json`);
  const body = JSON.stringify({ nonce, phase, ...data }) + '\n';
  if (exclusive) writeFileSync(file, body, { flag: 'wx', mode: 0o600 });
  else {
    const staging = `${file}.${randomBytes(16).toString('hex')}`;
    writeFileSync(staging, body, { flag: 'wx', mode: 0o600 });
    renameSync(staging, file);
  }
}

export function validateBranch(request, entries, leaf) {
  if (!/^[a-f0-9]{64}$/.test(request.transcriptSha256 ?? '') || leaf !== request.expectedLeaf) throw Error('snapshot mismatch');
  const indexed = new Map();
  for (const entry of entries) {
    if (entry.type === 'session') continue;
    if (!entry.id || indexed.has(entry.id)) throw Error('ambiguous entry identity');
    indexed.set(entry.id, entry);
  }
  const lineage = start => {
    const branch = []; const seen = new Set(); let id = start;
    while (id !== null) {
      if (seen.has(id) || !indexed.has(id)) throw Error('ambiguous lineage');
      seen.add(id); const entry = indexed.get(id); branch.push(entry); id = entry.parentId;
    }
    return branch.reverse();
  };
  const current = lineage(leaf);
  const root = current[0];
  if (root?.type !== 'custom' || root.customType !== 'pi-artifact-cleanup' || root.parentId !== null) throw Error('not detached cleanup branch');
  for (const entry of current) {
    const metadata = ['session_info', 'model_change', 'thinking_level_change'].includes(entry.type);
    const cleanup = entry.type === 'custom' && entry.customType === 'pi-artifact-cleanup';
    const banner = entry.type === 'custom_message' && ['context-lean', 'context-watchdog'].includes(entry.customType);
    if (!metadata && !cleanup && !banner) throw Error('current branch contains work');
  }
  const rootIndex = entries.indexOf(root);
  const lastWork = entries.slice(0, rootIndex).reverse().find(entry => entry.type === 'message');
  if (!lastWork || lastWork.id !== request.targetLeaf || entries[rootIndex - 1]?.id !== request.targetLeaf || lastWork.message.role === 'user') throw Error('not latest nonuser work leaf');
  const target = lineage(request.targetLeaf);
  if (!target.some(entry => entry.type === 'message' && entry.message.role === 'user')) throw Error('target has no human history');
  const model = [...target].reverse().find(entry => entry.type === 'model_change');
  const effort = [...target].reverse().find(entry => entry.type === 'thinking_level_change');
  if (model?.provider !== request.provider || model?.modelId !== request.model || effort?.thinkingLevel !== request.effort) throw Error('historical selection differs');
  return { root: root.id, currentEntries: current.length, targetEntries: target.length };
}

export function registerRecovery(pi, directory) {
  pi.registerCommand('context-branch-restore', {
    description: 'Explicit nonce-bound restoration of a detached metadata-only context branch',
    handler: async (nonce, ctx) => {
      let attempted = false;
      let ownedRequest = false;
      try {
        if (!/^[a-f0-9]{64}$/.test(nonce)) throw Error('invalid nonce');
        privatePath(directory, true);
        const path = join(directory, `${nonce}.request.json`); privatePath(path);
        const request = JSON.parse(readFileSync(path, 'utf8'));
        if (request.nonce !== nonce) throw Error('nonce mismatch');
        ownedRequest = true;
        if (typeof request.role !== 'string' || !request.role) throw Error('role required');
        const observe = () => ({ pid: process.pid, install: process.env.PI_FLEET_INSTALL,
          role: process.env.PI_ROLE_RESOLVED, session: ctx.sessionManager.getSessionFile(), cwd: ctx.cwd,
          provider: ctx.model?.provider, model: ctx.model?.id, effort: pi.getThinkingLevel(),
          idle: ctx.isIdle(), pending: ctx.hasPendingMessages(), editor: ctx.hasUI ? ctx.ui.getEditorText() : null });
        const before = observe(); qualify(request, before);
        const original = readFileSync(request.session);
        if (digest(original) !== request.transcriptSha256) throw Error('snapshot mismatch');
        const entries = original.toString('utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
        const proof = validateBranch(request, entries, ctx.sessionManager.getLeafId());
        qualify(request, observe());
        if (digest(readFileSync(request.session)) !== request.transcriptSha256 || ctx.sessionManager.getLeafId() !== request.expectedLeaf) throw Error('snapshot changed');
        // Exclusive durable intent: any outcome after this point must be inspected, never blindly resent.
        receipt(directory, nonce, 'intent', { before: { ...before, editor: undefined }, expectedLeaf: request.expectedLeaf,
          targetLeaf: request.targetLeaf, transcriptSha256: request.transcriptSha256, proof }, true);
        attempted = true;
        const result = await ctx.navigateTree(request.targetLeaf, { summarize: false });
        if (result.cancelled || result.aborted || ctx.sessionManager.getLeafId() !== request.targetLeaf) throw Error('navigation not proven');
        qualify(request, observe());
        if (!readFileSync(request.session).equals(original)) throw Error('unexpected transcript mutation');
        // branch() is memory-only. A native audit append persists the restored parent across restart.
        pi.appendEntry('pi-context-branch-recovery', { nonce, previousLeaf: request.expectedLeaf,
          targetLeaf: request.targetLeaf, originalSha256: request.transcriptSha256,
          provider: request.provider, model: request.model, effort: request.effort });
        const after = observe(); qualify(request, after);
        const restored = ctx.sessionManager.getBranch(); const last = restored.at(-1);
        if (last?.customType !== 'pi-context-branch-recovery' || last.parentId !== request.targetLeaf || last.data?.nonce !== nonce) throw Error('durable branch not proven');
        const bytes = readFileSync(request.session);
        if (!bytes.subarray(0, original.length).equals(original)) throw Error('history prefix changed');
        receipt(directory, nonce, 'restored', { before: { ...before, editor: undefined }, after: { ...after, editor: undefined },
          previousLeaf: request.expectedLeaf, targetLeaf: request.targetLeaf, persistedLeaf: last.id,
          originalSha256: request.transcriptSha256, restoredSha256: digest(bytes), branchEntries: restored.length });
      } catch {
        // No error/payload/draft content in operational records or notifications.
        if (ownedRequest) receipt(directory, nonce, attempted ? 'uncertain' : 'refused', { navigationAttempted: attempted });
        ctx.ui.notify(attempted ? 'Context restoration outcome requires inspection; do not resend.' : 'Context restoration refused before navigation.', 'warning');
      }
    },
  });
}

export default function (pi) {
  const directory = join(homedir(), '.local/state/throttler-recovery-20261111/context-branch-recovery');
  registerRecovery(pi, directory);
  privatePath(directory, true);
  receipt(directory, String(process.pid), 'loaded', { pid: process.pid, version: 'context-branch-native-v1',
    generation: randomBytes(32).toString('hex'), loadedAt: Date.now() });
}
