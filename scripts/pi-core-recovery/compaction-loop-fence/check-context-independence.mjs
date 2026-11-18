// Real installed loader/runner, synthetic private request, no live pane/provider.
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, lstatSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { qualifiedRuntime } from './fence.mjs';

const install = resolve(process.argv[2]);
const native = await qualifiedRuntime(install);
const before = native.AgentSession.prototype._runAutoCompaction;
const load = name => import(pathToFileURL(resolve(install, 'dist/core/extensions', name)));
const { loadExtensions, createExtensionRuntime } = await load('loader.js');
const { ExtensionRunner } = await load('runner.js');
const home = mkdtempSync(join(tmpdir(), 'fence-native-command-'));
process.env.HOME = home; process.env.PI_FLEET_INSTALL = install;
try {
  const directory = join(home, '.local/state/throttler-recovery-20261111/core-upgrades');
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const runtime = createExtensionRuntime(); runtime.getThinkingLevel = () => 'max';
  const loaded = await loadExtensions([resolve(import.meta.dirname, 'extension.ts')], '/synthetic', undefined, runtime);
  assert.deepEqual(loaded.errors, []);
  assert.equal(native.AgentSession.prototype._runAutoCompaction, before, 'loading cannot patch anything');
  assert.equal(loaded.extensions[0].handlers.size, 0, 'no startup/event hooks');
  const runner = new ExtensionRunner(loaded.extensions, runtime, '/synthetic', { getSessionFile: () => '/synthetic.jsonl', getBranch: () => { throw Error('FORBIDDEN history inspection'); }, getLeafId: () => { throw Error('FORBIDDEN leaf inspection'); }, appendCustomEntry: () => { throw Error('FORBIDDEN history mutation'); }, navigateTree: () => { throw Error('FORBIDDEN navigation'); } }, {});
  let idle = false; let pending = false; let editor = '';
  runner.getModel = () => ({ provider: 'claude-lane', id: 'claude-opus-5-5' });
  runner.isIdleFn = () => idle; runner.hasPendingMessagesFn = () => pending;
  runner.hasUI = () => true; runner.uiContext = { getEditorText: () => editor };
  assert.equal(lstatSync(join(directory, `${process.pid}.compaction-fence-loaded.json`)).mode & 0o777, 0o600);
  const registration = JSON.parse(readFileSync(join(directory, `${process.pid}.compaction-fence-loaded.json`)));
  assert.equal(registration.pid, process.pid);
  assert.match(registration.generation, /^[a-f0-9]{64}$/);
  const nonce = 'a'.repeat(64);
  const request = { schema: 1, nonce, expires: Date.now() + 60000, pid: process.pid,
    session: '/synthetic.jsonl', cwd: '/synthetic', provider: 'claude-lane', model: 'claude-opus-5-5', effort: 'max', install };
  writeFileSync(join(directory, `${nonce}.request.json`), JSON.stringify(request), { mode: 0o600 });
  const invoke = (action = 'enable') => runner.getCommand(`compaction-loop-fence-${action}`).handler(nonce, runner.createCommandContext());
  assert.equal(loaded.extensions[0].commands.size, 2);
  await assert.rejects(invoke(), /not empty and idle/);
  idle = true; pending = true; await assert.rejects(invoke(), /not empty and idle/);
  pending = false; editor = 'human draft'; await assert.rejects(invoke(), /not empty and idle/);
  assert.equal(native.AgentSession.prototype._runAutoCompaction, before, 'all unsafe commands leave prototype intact');
  for (const action of ['enable', 'disable']) {
    idle = false; editor = ''; await assert.rejects(invoke(action), /not empty and idle/);
    idle = true; pending = true; await assert.rejects(invoke(action), /not empty and idle/);
    pending = false; editor = 'human draft'; await assert.rejects(invoke(action), /not empty and idle/);
  }
  editor = '';
  const saveRequest = value => writeFileSync(join(directory, `${nonce}.request.json`), JSON.stringify(value), { mode: 0o600 });
  for (const [field, bad] of [['pid', process.pid + 1], ['session', '/wrong.jsonl'], ['cwd', '/wrong'], ['model', 'wrong'], ['effort', 'high'], ['install', '/wrong'], ['provider', 'wrong']]) {
    saveRequest({ ...request, [field]: bad });
    await assert.rejects(invoke(), /identity mismatch/);
    assert.equal(native.AgentSession.prototype._runAutoCompaction, before);
  }
  saveRequest({ ...request, expires: Date.now() - 1 });
  await assert.rejects(invoke(), /expired request/);
  saveRequest(request);
  await invoke();
  assert.notEqual(native.AgentSession.prototype._runAutoCompaction, before);
  assert.equal(JSON.parse(readFileSync(join(directory, `${nonce}.compaction-fence.json`))).enabled, true);
  await invoke('disable');
  assert.equal(JSON.parse(readFileSync(join(directory, `${nonce}.compaction-fence.json`))).enabled, false);
  console.log(JSON.stringify({ install, nativeLoader: true, nativeRunner: true, unsafeStatesRefused: 6, registeredCommands: 2, freshRegistrationReceipt: true, noStartupPatch: true, historyAccessForbidden: true, identityNearMissesRefused: 7, expiredRefused: true, passed: true }));
} finally { rmSync(home, { recursive: true, force: true }); }
