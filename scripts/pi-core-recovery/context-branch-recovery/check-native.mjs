// Actual installed loader, command runner, SessionManager and navigateTree. Synthetic files only.
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const extension = resolve(import.meta.dirname, 'restore.ts');
for (const install of process.argv.slice(2).map(resolvePath => resolve(resolvePath))) {
  const load = name => import(pathToFileURL(join(install, 'dist/core', `${name}.js`)));
  const { SessionManager } = await load('session-manager');
  const { AgentSession } = await load('agent-session');
  const { loadExtensions, createExtensionRuntime } = await load('extensions/loader');
  const { ExtensionRunner } = await load('extensions/runner');
  const home = mkdtempSync(join(tmpdir(), 'native-context-recovery-'));
  const saved = { HOME: process.env.HOME, install: process.env.PI_FLEET_INSTALL, role: process.env.PI_ROLE_RESOLVED };
  process.env.HOME = home; process.env.PI_FLEET_INSTALL = install; process.env.PI_ROLE_RESOLVED = 'eng~';
  try {
    const directory = join(home, '.local/state/throttler-recovery-20261111/context-branch-recovery');
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    let count = 0;
    let previousGeneration;
    const run = async (patch = {}, state = {}, mutate = () => {}, expected = false, historicalEffort = 'max') => {
      const nonce = (++count).toString(16).padStart(64, '0');
      const transcript = join(home, `${nonce}.jsonl`);
      const manager = SessionManager.open(transcript);
      manager.appendModelChange('claude-lane', 'claude-opus-5-5'); manager.appendThinkingLevelChange(historicalEffort);
      manager.appendMessage({ role: 'user', content: 'synthetic existing work', timestamp: 1 });
      const target = manager.appendMessage({ role: 'assistant', content: [{ type: 'text', text: 'synthetic result' }],
        api: 'anthropic-messages', provider: 'claude-lane', model: 'claude-opus-5-5', stopReason: 'stop', timestamp: 2,
        usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
      manager.resetLeaf(); manager.appendCustomEntry('pi-artifact-cleanup', { phase: 'shutdown' });
      mutate(manager);
      const before = readFileSync(transcript); const oldLeaf = manager.getLeafId();
      const runtime = createExtensionRuntime(); runtime.getThinkingLevel = () => 'max';
      runtime.appendEntry = (type, data) => manager.appendCustomEntry(type, data);
      for (const name of ['setModel', 'setThinkingLevel', 'sendMessage', 'sendUserMessage']) runtime[name] = () => { throw Error(`forbidden ${name}`); };
      const loaded = await loadExtensions([extension], home, undefined, runtime);
      assert.deepEqual(loaded.errors, []);
      const loadedReceipt = JSON.parse(readFileSync(join(directory, `${process.pid}.loaded.json`), 'utf8'));
      assert.equal(loadedReceipt.pid, process.pid);
      assert.equal(loadedReceipt.version, 'context-branch-native-v1');
      assert.match(loadedReceipt.generation, /^[a-f0-9]{64}$/);
      assert.notEqual(loadedReceipt.generation, previousGeneration);
      previousGeneration = loadedReceipt.generation;
      const runner = new ExtensionRunner(loaded.extensions, runtime, home, manager, {});
      const model = { provider: 'claude-lane', id: 'claude-opus-5-5' };
      let navigations = 0;
      const native = Object.create(AgentSession.prototype);
      const agent = { state: { model, thinkingLevel: 'max', messages: [] },
        replaceMessages: messages => { agent.state.messages = messages; },
        streamFunction: () => { throw Error('inference forbidden'); } };
      Object.assign(native, { agent, sessionManager: manager, _extensionRunner: runner,
        _entryIdsByMessage: new WeakMap(), _promptPreflightCount: 0,
        _restoreToolsFromTranscript: () => {}, _resolveIdleWaitIfIdle: () => {}, _emit: () => {} });
      runner.getModel = () => model; runner.getScopedModels = () => [];
      runner.isIdleFn = () => state.idle ?? true; runner.hasPendingMessagesFn = () => state.pending ?? false;
      runner.uiContext = { getEditorText: () => state.editor ?? '', notify: () => {} }; runner.hasUI = () => true;
      runner.navigateTreeHandler = (...args) => { navigations++; return native.navigateTree(...args); };
      const request = { schema: 1, nonce, expires: Date.now() + 60000, pid: process.pid,
        session: transcript, cwd: home, provider: model.provider, model: model.id, effort: 'max', install, role: 'eng~',
        expectedLeaf: oldLeaf, targetLeaf: target, transcriptSha256: sha(before), ...patch };
      writeFileSync(join(directory, `${nonce}.request.json`), JSON.stringify(request), { mode: 0o600 });
      const command = runner.getCommand('context-branch-restore');
      await command.handler(nonce, runner.createCommandContext());
      const success = existsSync(join(directory, `${nonce}.restored.json`));
      assert.equal(success, expected, 'native restoration verdict');
      if (!success) { assert.equal(navigations, 0); assert(readFileSync(transcript).equals(before)); return; }
      assert.equal(navigations, 1); assert.equal(native.model, model); assert.equal(native.thinkingLevel, 'max');
      const restored = SessionManager.open(transcript);
      assert.equal(restored.getLeafEntry().parentId, target);
      assert(restored.getBranch().some(e => e.type === 'message' && e.message.role === 'user'));
      assert.equal(restored.buildSessionContext().model.modelId, model.id);
      assert.equal(restored.buildSessionContext().thinkingLevel, 'max');
      assert(readFileSync(transcript).subarray(0, before.length).equals(before));
      await command.handler(nonce, runner.createCommandContext()); assert.equal(navigations, 1, 'never resend navigation');
    };
    await run({}, {}, () => {}, true);
    for (const patch of [{ expires: 1 }, { pid: -1 }, { model: 'other' }, { expectedLeaf: 'wrong' },
      { targetLeaf: 'missing' }, { transcriptSha256: '0'.repeat(64) }]) await run(patch);
    for (const state of [{ idle: false }, { pending: true }, { editor: 'private human draft' }]) await run({}, state);
    await run({}, {}, manager => manager.appendMessage({ role: 'user', content: 'new work', timestamp: 3 }));
    await run({ effort: 'high' });
    await run({}, {}, () => {}, false, 'high');
    console.log(JSON.stringify({ install, nativeLoader: true, freshPidLoadedReceipt: true, nativeNavigation: true, persistedReindex: true,
      selectionPreserved: true, nearMissesRefused: 12, inferenceCalls: 0, passed: true }));
  } finally {
    process.env.HOME = saved.HOME;
    if (saved.install === undefined) delete process.env.PI_FLEET_INSTALL; else process.env.PI_FLEET_INSTALL = saved.install;
    if (saved.role === undefined) delete process.env.PI_ROLE_RESOLVED; else process.env.PI_ROLE_RESOLVED = saved.role;
    rmSync(home, { recursive: true, force: true });
  }
}
