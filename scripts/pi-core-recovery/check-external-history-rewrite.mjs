// Offline causal oracle: only synthetic mkdtemp transcripts enter native SessionManager.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const helpers = ['pi-purge-retries', 'pi-repair-parents'].map(name => join(homedir(), '.local/bin', name));
const hash = value => createHash('sha256').update(value).digest('hex');
const installs = process.argv.slice(2);
assert.ok(installs.length, 'pass actual installed Pi roots explicitly');
const temporary = mkdtempSync(join(tmpdir(), 'pi-external-rewrite-'));
const read = file => readFileSync(file, 'utf8').trim().split('\n').map(JSON.parse);
const workIds = manager => manager.getBranch().filter(entry => entry.type === 'message').map(entry => entry.id);
function apply(helper, root, extra = []) {
  assert.ok(resolve(root).startsWith(temporary + '/'), 'helper root must be synthetic');
  const result = spawnSync('python3', [helper, '--root', root, '--apply', ...extra], { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr);
}
try {
  const results = [];
  for (const [index, install] of installs.entries()) {
    const { SessionManager } = await import(pathToFileURL(join(resolve(install), 'dist/core/session-manager.js')));
    const root = join(temporary, String(index));
    const directory = join(root, '.pi/agent/sessions/synthetic');
    mkdirSync(directory, { recursive: true });
    const file = join(directory, 'affected.jsonl');
    const controlFile = join(temporary, `control-${index}.jsonl`);
    const entries = [
      { type: 'session', version: 3, id: '00000000-0000-4000-8000-000000000001', cwd: temporary, timestamp: new Date().toISOString() },
      { type: 'message', id: 'human-work', parentId: null, timestamp: new Date().toISOString(), message: { role: 'user', content: 'synthetic unfinished assignment', timestamp: 1 } },
      { type: 'message', id: 'failed-leaf', parentId: 'human-work', timestamp: new Date().toISOString(), message: { role: 'assistant', content: [], stopReason: 'error', timestamp: 2 } },
    ];
    const original = entries.map(entry => JSON.stringify(entry)).join('\n') + '\n';
    writeFileSync(file, original); writeFileSync(controlFile, original);
    const live = SessionManager.open(file);
    const control = SessionManager.open(controlFile);
    assert.deepEqual(workIds(live), ['human-work', 'failed-leaf']);
    apply(helpers[0], root, ['--quiet']);
    assert.equal(live.getLeafId(), 'failed-leaf', 'external purge cannot update native memory');
    assert.ok(!read(file).some(entry => entry.id === live.getLeafId()), 'native leaf is now absent on disk');
    const cleanup = live.appendCustomEntry('pi-artifact-cleanup', { phase: 'synthetic-settled' });
    control.appendCustomEntry('pi-artifact-cleanup', { phase: 'synthetic-settled' });
    assert.equal(read(file).at(-1).parentId, 'failed-leaf', 'native append persists its stale parent');
    apply(helpers[1], root, ['--backup', join(root, 'nonexistent-backup')]);
    assert.equal(read(file).at(-1).parentId, null, 'missing backup causes parent repair to sever ancestry');
    assert.equal(live.getLeafId(), cleanup, 'running process has not been reindexed');
    assert.ok(workIds(live).includes('human-work'), 'live memory still retains work');
    const reopened = SessionManager.open(file);
    assert.deepEqual(workIds(reopened), [], 'restart selects detached cleanup root and loses active work context');
    assert.ok(read(file).some(entry => entry.id === 'human-work'), 'raw work remains present, only active ancestry lost');
    assert.deepEqual(workIds(SessionManager.open(controlFile)), ['human-work', 'failed-leaf'], 'untouched control preserves ancestry across restart');
    results.push({ install: resolve(install), staleLeafAfterPurge: true, danglingCleanup: true, repairedParentNull: true,
      rawWorkRetained: true, reopenedActiveWorkCount: workIds(reopened).length, controlWorkCount: 2 });
  }
  console.log(JSON.stringify({ mechanismReproduced: true, syntheticOnly: true, providerCalls: 0,
    rosterAttribution: false, helperSha256: Object.fromEntries(helpers.map(file => [file, hash(readFileSync(file))])), results }));
} finally { rmSync(temporary, { recursive: true, force: true }); }
