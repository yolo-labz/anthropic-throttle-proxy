// Synthetic persistent fixture only; no real transcript or provider is opened.
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { digest, validateIncident } from './restore.ts';

for (const install of process.argv.slice(2)) {
  const load = name => import(pathToFileURL(resolve(install, `dist/core/${name}.js`)));
  const { SessionManager } = await load('session-manager');
  const { AgentSession } = await load('agent-session');
  const { loadExtensions, createExtensionRuntime } = await load('extensions/loader');
  const { ExtensionRunner } = await load('extensions/runner');
  const home = mkdtempSync(join(tmpdir(), 'horaco-native-incident-'));
  const saved = { HOME: process.env.HOME, PI_FLEET_INSTALL: process.env.PI_FLEET_INSTALL, PI_ROLE_RESOLVED: process.env.PI_ROLE_RESOLVED };
  Object.assign(process.env, { HOME: home, PI_FLEET_INSTALL: install, PI_ROLE_RESOLVED: 'orch' });
  try {
    const transcript = join(home, 'synthetic.jsonl');
    const manager = SessionManager.open(transcript);
    for (let n = 0; n < 10; n++) manager.appendMessage(n % 2 === 0 ?
      { role: 'user', content: 'synthetic human work', timestamp: n } :
      { role: 'assistant', content: [{ type: 'text', text: 'synthetic result' }], api: 'anthropic-messages',
        provider: 'claude-lane', model: 'claude-opus-5-5', stopReason: 'stop', timestamp: n,
        usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
    const target = manager.appendContextEdit(manager.getLeafId(), null);
    const entries = readFileSync(transcript, 'utf8').trim().split('\n').map(JSON.parse);
    entries.at(-1).id = '74edcb64';
    const prefix = Buffer.from(entries.map(JSON.stringify).join('\n') + '\n');
    const work = entries.filter(e => e.type === 'message').reverse().map(e => e.id);
    const suffix = [{ id: 'c5582014', parentId: '1fe7721e', type: 'custom', customType: 'pi-artifact-cleanup' },
      { id: 'a4bbb629', parentId: 'c5582014', type: 'custom', customType: 'pi-artifact-cleanup' }];
    let before = Buffer.concat([prefix, Buffer.from(suffix.map(JSON.stringify).join('\n') + '\n')]);
    suffix[0].parentId = null;
    const repairedBytes = Buffer.concat([prefix, Buffer.from(suffix.map(JSON.stringify).join('\n') + '\n')]);
    writeFileSync(transcript, repairedBytes);
    const reopened = SessionManager.open(transcript);
    const request = { schema: 1, nonce: 'a'.repeat(64), expires: Date.now() + 60000, pid: process.pid,
      session: transcript, cwd: home, provider: 'claude-lane', model: 'claude-opus-5-5', effort: 'max',
      install, role: 'orch', incident: 'home-w3f-p1-20261111', expectedLeaf: 'a4bbb629', targetLeaf: '74edcb64',
      transcriptSha256: digest(before), originalBytes: prefix.length, originalPrefixSha256: digest(prefix), originalBranchWorkIds: work };
    validateIncident(request, before, 'a4bbb629');
    Object.assign(request, { externalParentRepair: true, normalizedPrefixRows: entries.length, normalizedPrefixSha256: digest(prefix), normalizedCompactIds: entries.map(e => e.id).filter(Boolean), transcriptSha256: digest(repairedBytes) });
    before = repairedBytes;
    const startup = { id: 'startup-model', parentId: 'a4bbb629', type: 'model_change', provider: 'claude-lane', modelId: 'claude-opus-5-5' };
    const startupBytes = Buffer.concat([before, Buffer.from(JSON.stringify(startup) + '\n')]);
    validateIncident({ ...request, expectedLeaf: startup.id, transcriptSha256: digest(startupBytes) }, startupBytes, startup.id);
    for (const customType of ['context-lean', 'context-watchdog']) {
      const banner = { id: 'startup-banner', parentId: 'a4bbb629', type: 'custom_message', customType };
      const bannerBytes = Buffer.concat([before, Buffer.from(JSON.stringify(banner) + '\n')]);
      validateIncident({ ...request, expectedLeaf: banner.id, transcriptSha256: digest(bannerBytes) }, bannerBytes, banner.id);
    }
    for (const entry of [{ type: 'custom_message', customType: 'unknown' }, { type: 'message', message: { role: 'user' } }]) {
      const wrong = { ...entry, id: 'wrong', parentId: 'a4bbb629' };
      const wrongBytes = Buffer.concat([before, Buffer.from(JSON.stringify(wrong) + '\n')]);
      assert.throws(() => validateIncident({ ...request, expectedLeaf: wrong.id, transcriptSha256: digest(wrongBytes) }, wrongBytes, wrong.id));
    }
    const wrongStartup = Buffer.concat([before, Buffer.from(JSON.stringify({ ...startup, modelId: 'other' }) + '\n')]);
    assert.throws(() => validateIncident({ ...request, expectedLeaf: startup.id, transcriptSha256: digest(wrongStartup) }, wrongStartup, startup.id));
    for (const patch of [{ targetLeaf: target }, { expectedLeaf: 'other' }, { transcriptSha256: '0'.repeat(64) },
      { originalPrefixSha256: '0'.repeat(64) }, { originalBytes: prefix.length - 1 }, { originalBranchWorkIds: [] }]) {
      assert.throws(() => validateIncident({ ...request, ...patch }, before, 'a4bbb629'));
    }
    const directory = join(home, '.local/state/throttler-recovery-20261111/dangling-tail-recovery-v2');
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const runtime = createExtensionRuntime(); runtime.getThinkingLevel = () => 'max';
    runtime.appendEntry = (type, data) => reopened.appendCustomEntry(type, data);
    const loaded = await loadExtensions([resolve(import.meta.dirname, 'restore.ts')], home, undefined, runtime);
    assert.deepEqual(loaded.errors, []); assert.equal(loaded.extensions[0].handlers.size, 0);
    const registered = JSON.parse(readFileSync(join(directory, `${process.pid}.loaded.json`)));
    assert.equal(registered.pid, process.pid); assert.equal(registered.version, 'horaco-dangling-v2');
    assert.match(registered.generation, /^[a-f0-9]{64}$/);
    const runner = new ExtensionRunner(loaded.extensions, runtime, home, reopened, {});
    const model = { provider: 'claude-lane', id: 'claude-opus-5-5' };
    const native = Object.create(AgentSession.prototype);
    Object.assign(native, { agent: { state: { model, thinkingLevel: 'max', messages: [] } }, sessionManager: reopened,
      _extensionRunner: runner, _entryIdsByMessage: new WeakMap(), _promptPreflightCount: 0,
      _restoreToolsFromTranscript: () => {}, _resolveIdleWaitIfIdle: () => {}, _emit: () => {} });
    runner.getModel = () => model; runner.getScopedModels = () => [];
    let idle = false; let pending = false; let editor = ''; let calls = 0;
    runner.isIdleFn = () => idle; runner.hasPendingMessagesFn = () => pending;
    runner.hasUI = () => true; runner.uiContext = { getEditorText: () => editor };
    runner.navigateTreeHandler = (...args) => { calls++; return native.navigateTree(...args); };
    writeFileSync(join(directory, `${request.nonce}.request.json`), JSON.stringify(request), { mode: 0o600 });
    const command = () => runner.getCommand('dangling-tail-restore').handler(request.nonce, runner.createCommandContext());
    await assert.rejects(command()); idle = true; pending = true; await assert.rejects(command());
    pending = false; editor = 'private draft'; await assert.rejects(command()); editor = '';
    assert.equal(calls, 0); await command(); assert.equal(calls, 1);
    assert.equal(native.model, model); assert.equal(native.thinkingLevel, 'max');
    assert(readFileSync(transcript).subarray(0, before.length).equals(before));
    const persisted = SessionManager.open(transcript);
    assert.equal(persisted.getLeafEntry().parentId, '74edcb64');
    assert(work.every(id => persisted.getBranch().some(e => e.id === id)));
    await assert.rejects(command()); assert.equal(calls, 1);
    console.log(JSON.stringify({ install, actualNativeNavigation: true, contextEditTarget: true,
      persistedReopen: true, allTenWorkPreserved: true, nearMisses: 12, noInference: true, passed: true }));
  } finally { rmSync(home, { recursive: true, force: true }); Object.assign(process.env, saved); }
}
