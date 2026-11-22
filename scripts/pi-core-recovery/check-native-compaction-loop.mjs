#!/usr/bin/env node
// Native class, synthetic in-memory branch, no provider, file sessions, or inference.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [install, expected = 'exposed'] = process.argv.slice(2);
assert(install && ['exposed', 'fenced'].includes(expected), 'usage: INSTALL exposed|fenced');
const load = (name) => import(pathToFileURL(resolve(install, `dist/core/${name}.js`)));
const { AgentSession } = await load('agent-session');
const { SessionManager } = await load('session-manager');
const sessionManager = SessionManager.inMemory();
for (let n = 0; n < 4; n++) {
  sessionManager.appendMessage({ role: 'user', content: [{ type: 'text', text: `synthetic ${n} `.repeat(40) }], timestamp: n + 1 });
}
const fingerprints = [];
const native = Object.create(AgentSession.prototype);
Object.assign(native, {
  agent: { state: { thinkingLevel: 'max', model: { provider: 'claude-lane', id: 'claude-opus-5-5', contextWindow: 1000000, maxTokens: 8192 } },
    streamFunction: (_model, context) => {
      fingerprints.push(createHash('sha256').update(JSON.stringify(context.messages.map(message => ({ role: message.role, content: message.content })))).digest('hex'));
      return { result: async () => ({ role: 'assistant', content: [], stopReason: 'error', errorMessage: 'Request deadline exceeded' }) };
    } },
  sessionManager,
  settingsManager: { getCompactionSettings: () => ({ enabled: true, reserveTokens: 800000, keepRecentTokens: 1 }),
    getRetrySettings: () => ({ enabled: false }) },
  _extensionRunner: { hasHandlers: () => false },
  _emit: () => {},
  _emitSessionCompactFailed: async () => {},
  _resolveIdleWaitIfIdle: () => {},
  _getSummarizationRequestAuth: async model => ({ model, thinkingLevel: 'max' }),
});
const before = sessionManager.getLeafId();
for (let attempt = 0; attempt < 3; attempt++) await native._runAutoCompaction('threshold', false);
assert.equal(sessionManager.getLeafId(), before, 'failed summaries must not mutate synthetic history');
assert.equal(new Set(fingerprints).size, 1, 'all dispatched summaries have the same preparation');
assert.equal(fingerprints.length, expected === 'exposed' ? 3 : 1, 'unchanged failed preparation must not be dispatched repeatedly');
console.log(JSON.stringify({ install, expected, dispatches: fingerprints.length, distinctPreparations: new Set(fingerprints).size, passed: true }));
