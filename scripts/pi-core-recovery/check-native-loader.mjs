// No Pi session or provider is started. Exercise the exact installed extension loader/runner.
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { nativeContext } from './core-upgrade-guard.ts';

const [install, extension] = process.argv.slice(2).map((path) => resolve(path));
const load = (file) => import(pathToFileURL(join(install, 'dist/core/extensions', file)).href);
const { loadExtensions, createExtensionRuntime } = await load('loader.js');
const { ExtensionRunner } = await load('runner.js');
const { SessionManager } = await import(pathToFileURL(join(install, 'dist/core/session-manager.js')));
const home = mkdtempSync(join(tmpdir(), 'core-native-loader-'));
process.env.HOME = home;
process.env.PI_FLEET_INSTALL = install;
process.argv = [process.execPath, `${install}/dist/cli.js`, '--name', 'review'];
try {
  const runtime = createExtensionRuntime();
  runtime.getThinkingLevel = () => 'max';
  for (const name of ['setModel', 'setThinkingLevel', 'sendMessage', 'sendUserMessage']) {
    runtime[name] = () => { throw Error(`forbidden action: ${name}`); };
  }
  const loaded = await loadExtensions([extension], '/project', undefined, runtime);
  assert.deepEqual(loaded.errors, []);
  assert.equal(loaded.extensions.length, 1);
  const transcript = join(home, 'synthetic.jsonl');
  const original = JSON.stringify({ type: 'session', version: 3, id: '00000000-0000-4000-8000-000000000001', cwd: '/project', timestamp: new Date().toISOString() }) + '\n' +
    JSON.stringify({ type: 'message', id: 'work', parentId: null, timestamp: new Date().toISOString(), message: { role: 'user', content: 'synthetic', timestamp: 1 } }) + '\n';
  writeFileSync(transcript, original);
  const manager = SessionManager.open(transcript);
  const runner = new ExtensionRunner(loaded.extensions, runtime, '/project', manager, {});
  let editor = '';
  let pending = false;
  let idle = true;
  let shutdowns = 0;
  runner.getModel = () => ({ provider: 'claude-lane', id: 'claude-fable-5-2' });
  runner.getScopedModels = () => [];
  runner.isIdleFn = () => idle;
  runner.hasPendingMessagesFn = () => pending;
  runner.uiContext = { getEditorText: () => editor, notify: () => {} };
  runner.hasUI = () => true;
  runner.shutdownHandler = () => { shutdowns++; };
  const directory = join(home, '.local/state/throttler-recovery-20261111/core-upgrades');
  mkdirSync(directory, { recursive: true });
  let count = 0;
  const invoke = async (name = 'core-upgrade-ready', expected = nativeContext(manager)) => {
    const nonce = (++count).toString(16).padStart(64, '0');
    const request = { schema: 1, nonce, expires: Date.now() + 60000, pid: process.pid,
      session: transcript, cwd: '/project', provider: 'claude-lane', model: 'claude-fable-5-2', effort: 'max', install,
      expectedNativeContext: expected,
      expectedBootstrapHash: createHash('sha256').update(JSON.stringify(process.argv)).digest('hex') };
    writeFileSync(join(directory, `${nonce}.request.json`), JSON.stringify(request), { mode: 0o600 });
    await runner.getCommand(name).handler(nonce, runner.createCommandContext());
    return nonce;
  };
  editor = 'human draft'; await invoke();
  editor = ''; pending = true; await invoke();
  pending = false; idle = false; await invoke();
  assert.equal(shutdowns, 0);
  idle = true;
  const stateNonce = await invoke('core-upgrade-state');
  assert.equal(shutdowns, 0);
  assert.equal(JSON.parse(readFileSync(join(directory, `${stateNonce}.state.json`))).effort, 'max');
  manager.leafId = 'absent-from-disk';
  const missingNonce = await invoke();
  assert.equal(shutdowns, 0, 'missing in-memory leaf must refuse BEFORE shutdown');
  assert.equal(JSON.parse(readFileSync(join(directory, `${missingNonce}.refused.json`))).reason, 'native-context-mismatch');
  manager.leafId = 'work';
  writeFileSync(transcript, original + JSON.stringify({ type: 'custom', id: 'dangling', parentId: 'missing-parent', customType: 'pi-artifact-cleanup' }) + '\n');
  const danglingNonce = await invoke();
  assert.equal(JSON.parse(readFileSync(join(directory, `${danglingNonce}.refused.json`))).reason, 'native-context-mismatch');
  assert.equal(shutdowns, 0, 'native context mismatch preserves the running instance');
  writeFileSync(transcript, original);
  const beforeChange = nativeContext(manager);
  manager.appendCustomEntry('synthetic-change', {});
  const changedNonce = await invoke('core-upgrade-ready', beforeChange);
  assert.equal(shutdowns, 0, 'changed agreed branch must require a fresh STATE proof');
  assert.equal(JSON.parse(readFileSync(join(directory, `${changedNonce}.refused.json`))).reason, 'native-context-changed');
  const readyNonce = await invoke();
  assert.equal(shutdowns, 1);
  assert.equal(JSON.parse(readFileSync(join(directory, `${readyNonce}.ready.json`))).model, 'claude-fable-5-2');
  console.log(JSON.stringify({ install, nativeLoader: true, nativeRunner: true, unsafeStatesRefused: 3, nativeDiskMismatchesRefused: 2, stateDoesNotQuit: true, gracefulShutdowns: shutdowns, passed: true }));
} finally { rmSync(home, { recursive: true, force: true }); }
