#!/usr/bin/env node
// Offline oracle against the installed native class; no agent/provider is constructed.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [install, expectation = 'safe'] = process.argv.slice(2);
if (!install || !['safe', 'exposed'].includes(expectation)) {
  throw new Error('usage: check-native-settlement.mjs INSTALL [safe|exposed]');
}
const { AgentSession } = await import(pathToFileURL(resolve(install, 'dist/core/agent-session.js')));
function gate() {
  let release;
  const promise = new Promise((done) => { release = done; });
  return { promise, release };
}
const hook = gate();
const nestedHook = gate();
const deferred = gate();
const deferredStarted = gate();
let emitted = 0;
let waitReturned = false;
let lateWaitReturned = false;
const native = Object.create(AgentSession.prototype);
Object.assign(native, {
  _isAgentRunActive: true,
  _isEmittingAgentSettled: false,
  _settlementDepth: 0,
  _agentRunAbortRequested: false,
  _steeringMessages: [],
  _followUpMessages: [],
  _extensionRunner: { emit: () => (++emitted === 1 ? hook.promise : nestedHook.promise) },
  _emit: () => {},
  _deferredSettledActions: [
    async () => { await native._emitAgentSettled(); },
    async () => { deferredStarted.release(); await deferred.promise; },
  ],
});
const settling = native._emitAgentSettled();
const waiting = native.waitForIdle().then(() => { waitReturned = true; });
await Promise.resolve();
const duringHook = { nativeIdle: native.isIdle, nativePending: native.pendingMessageCount, waitReturned };
hook.release();
nestedHook.release();
await deferredStarted.promise;
const lateWaiting = native.waitForIdle().then(() => { lateWaitReturned = true; });
await Promise.resolve();
const duringDeferred = { nativeIdle: native.isIdle, waitReturned, lateWaitReturned };
deferred.release();
await settling;
await Promise.all([waiting, lateWaiting]);
assert.equal(waitReturned && lateWaitReturned, true, 'waiters must resolve after the full outer drain');
assert.equal(native._settlementDepth, 0, 'nested settlement depth must balance');
native._extensionRunner = { emit: async () => { throw new Error('settlement hook failure'); } };
native._deferredSettledActions = [];
const failedSettling = native._emitAgentSettled();
const afterFailure = native.waitForIdle();
const errorResults = await Promise.allSettled([failedSettling, afterFailure]);
assert.deepEqual(errorResults.map((result) => result.status), ['rejected', 'fulfilled']);
assert.equal(native._settlementDepth, 0, 'settlement failure must balance depth and release idle waiters');
const exposed = duringHook.waitReturned || duringDeferred.waitReturned || duringDeferred.lateWaitReturned;
process.stdout.write(`${JSON.stringify({ expectation, duringHook, duringDeferred, exposed })}\n`);
assert.equal(exposed, expectation === 'exposed', 'native wait must cover settled hooks and nested deferred actions');
assert.equal(duringHook.nativeIdle && duringDeferred.nativeIdle, true, 'existing isIdle hook semantics must remain');
