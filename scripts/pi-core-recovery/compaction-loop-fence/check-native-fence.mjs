import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { installFence, qualifiedRuntime } from './fence.mjs';

const unknown = mkdtempSync(resolve(tmpdir(), 'unknown-fence-native-'));
try {
  mkdirSync(resolve(unknown, 'dist/core/compaction'), { recursive: true });
  for (const file of ['agent-session.js', 'compaction/compaction.js']) writeFileSync(resolve(unknown, 'dist/core', file), 'unreviewed');
  await assert.rejects(qualifiedRuntime(unknown), /unreviewed native compaction source/);
} finally { rmSync(unknown, { recursive: true, force: true }); }

for (const install of process.argv.slice(2)) {
  const runtime = await qualifiedRuntime(install);
  const { SessionManager } = await import(pathToFileURL(resolve(install, 'dist/core/session-manager.js')));
  let dispatches = 0;
  const make = () => {
    const sessionManager = SessionManager.inMemory();
    for (let n = 0; n < 4; n++) sessionManager.appendMessage({ role: 'user', content: [{ type: 'text', text: `synthetic ${n} `.repeat(40) }], timestamp: n + 1 });
    const session = Object.create(runtime.AgentSession.prototype);
    Object.assign(session, {
      agent: { state: { thinkingLevel: 'max', model: { provider: 'claude-lane', id: 'claude-opus-5-5', contextWindow: 1000000 } },
        streamFunction: () => { dispatches++; return { result: async () => ({ role: 'assistant', content: [], stopReason: 'error', errorMessage: 'Request deadline exceeded' }) }; } },
      sessionManager,
      settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 800000, keepRecentTokens: 1 }), getRetrySettings: () => ({ enabled: false }) },
      _extensionRunner: { hasHandlers: () => false },
      _getSummarizationRequestAuth: async model => ({ model, thinkingLevel: 'max' }),
      _summarizationRetryCallbacks: () => undefined,
      _emit: () => {}, _emitSessionCompactFailed: async () => {}, _resolveIdleWaitIfIdle: () => {},
    });
    return session;
  };
  const unfenced = make();
  for (let n = 0; n < 3; n++) await unfenced._runAutoCompaction('threshold', false);
  assert.equal(dispatches, 3, 'actual original native loop dispatches three identical summaries');
  const controller = installFence(runtime);
  assert.equal(installFence(runtime), controller, 'idempotent installation');
  const session = make(); controller.enable(session.sessionManager);
  const before = session.sessionManager.getLeafId(); const stream = session.agent.streamFunction;
  dispatches = 0;
  for (let n = 0; n < 3; n++) await session._runAutoCompaction('threshold', false);
  assert.equal(dispatches, 1);
  assert.equal(session.sessionManager.getLeafId(), before);
  assert.equal(session.agent.streamFunction, stream, 'never replace the active stream function');
  session.sessionManager.appendCustomEntry('synthetic-metadata', { usage: 999 });
  await session._runAutoCompaction('threshold', false);
  assert.equal(dispatches, 1, 'metadata-only leaf changes are not fresh summary input');
  session.agent.state.thinkingLevel = 'high';
  await session._runAutoCompaction('threshold', false); assert.equal(dispatches, 2, 'effort change is new input');
  session.sessionManager.appendMessage({ role: 'user', content: [{ type: 'text', text: 'new material' }], timestamp: 8 });
  await session._runAutoCompaction('threshold', false); assert.equal(dispatches, 3, 'new content is new input');
  controller.disable(session.sessionManager);
  const disabledCount = dispatches;
  for (let n = 0; n < 2; n++) await session._runAutoCompaction('threshold', false);
  assert.equal(dispatches, disabledCount + 2, 'scoped disable restores original native dispatch');
  const authFailure = make(); controller.enable(authFailure.sessionManager);
  let authCalls = 0;
  authFailure._getSummarizationRequestAuth = async () => { authCalls++; throw Error('auth unavailable'); };
  for (let n = 0; n < 2; n++) await authFailure._runAutoCompaction('threshold', false);
  assert.equal(authCalls, 2, 'pre-dispatch failures never poison the fence');
  const racing = make(); controller.enable(racing.sessionManager);
  racing._getSummarizationRequestAuth = async model => {
    racing.agent.state.thinkingLevel = 'high';
    return { model, thinkingLevel: 'max' };
  };
  const raceBefore = dispatches;
  await racing._runAutoCompaction('threshold', false);
  racing.agent.state.thinkingLevel = 'max';
  await racing._runAutoCompaction('threshold', false);
  assert.equal(dispatches, raceBefore + 1, 'failure retains original input identity across asynchronous auth');
  const cancelled = make(); controller.enable(cancelled.sessionManager);
  let cancellations = 0;
  cancelled._extensionRunner = { hasHandlers: () => true, emit: async () => { cancellations++; return { cancel: true }; } };
  const cancelBefore = dispatches;
  for (let n = 0; n < 2; n++) await cancelled._runAutoCompaction('threshold', false);
  assert.equal(cancellations, 2); assert.equal(dispatches, cancelBefore, 'extension cancellation never dispatches or poisons the fence');
  const other = make(); const count = dispatches;
  for (let n = 0; n < 2; n++) await other._runAutoCompaction('threshold', false);
  assert.equal(dispatches, count + 2, 'unenrolled sessions remain native');
  const preparation = runtime.prepareCompaction(session.sessionManager.getBranch(), session.settingsManager.getCompactionSettings());
  await assert.rejects(session._runDefaultCompaction(preparation, session.model, 'explicit focus', new AbortController().signal, 'manual'));
  assert.equal(dispatches, count + 3, 'explicit manual default bypasses automatic suppression');
  const success = make(); controller.enable(success.sessionManager);
  const oldLeaf = success.sessionManager.getLeafId();
  await success._runAutoCompaction('threshold', false);
  success.agent.state.thinkingLevel = 'high';
  success.agent.hasQueuedMessages = () => false;
  success._refreshFinalizedContext = () => {};
  success._extensionRunner.emit = async () => {};
  success.agent.streamFunction = () => { dispatches++; return { result: async () => ({ role: 'assistant', stopReason: 'stop', content: [{ type: 'text', text: 'synthetic summary' }] }) }; };
  await success._runAutoCompaction('threshold', false);
  assert.equal(success.sessionManager.getLeafEntry().type, 'compaction', 'actual native successful append occurred');
  success.sessionManager.branch(oldLeaf);
  success.agent.state.thinkingLevel = 'max';
  const afterSuccess = dispatches;
  await success._runAutoCompaction('threshold', false);
  assert.equal(dispatches, afterSuccess + 1, 'successful native compaction cleared old failure');
  const aborted = make(); controller.enable(aborted.sessionManager);
  const abortedLeaf = aborted.sessionManager.getLeafId();
  const abortedBefore = dispatches;
  const response = { role: 'assistant', content: [], stopReason: 'aborted' };
  const responseStream = { result: async () => response };
  aborted.agent.streamFunction = () => { dispatches++; return responseStream; };
  for (let n = 0; n < 3; n++) await aborted._runAutoCompaction('threshold', false);
  assert.equal(dispatches, abortedBefore + 1, 'provider aborted response fenced without local signal cancellation');
  assert.equal(aborted.sessionManager.getLeafId(), abortedLeaf, 'provider abort must never append empty summary');
  assert.deepEqual(response, { role: 'assistant', content: [], stopReason: 'aborted' });
  assert.equal(await responseStream.result(), response, 'original stream and response remain unchanged');
  aborted.abort = async () => {};
  aborted._refreshFinalizedContext = () => {};
  aborted._extensionRunner.emit = async () => {};
  aborted.agent.streamFunction = () => { dispatches++; return { result: async () => ({ role: 'assistant', stopReason: 'stop', content: [{ type: 'text', text: 'explicit recovered summary' }] }) }; };
  const manualResult = await aborted.compact();
  assert.equal(manualResult.summary, 'explicit recovered summary');
  assert.equal(aborted.sessionManager.getLeafEntry().type, 'compaction', 'explicit native manual recovery appends real summary');
  console.log(JSON.stringify({ install, originalDispatches: 3, unchangedFencedDispatches: 1, nativeClass: true, passed: true }));
}
