// Offline candidate: only an explicit qualified command may enroll a session.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const VERSION = 'prepared-dispatch-fence-v1';
const MARKER = Symbol.for('pi.explicit.compaction-loop-fence');
const APPROVED = new Set([
  'babe3991f81701aec3b19362e57bb1612dd44069d2bf66193072bab8692650a3:b1d5b11efe4c997e85c80d7df7f18b84f0323cbd68a069b6aaafa8a922ddeafd',
  'b4412035114f516e690e51403035012b21f9d610f5c00d802831b3a740033e03:f51e78ef1da66b5803fc91e185f07b968f8f989ab9326b7744958416aad3f6f6',
]);
const digest = value => createHash('sha256').update(value).digest('hex');

export async function qualifiedRuntime(install) {
  const file = name => resolve(install, 'dist/core', name);
  const fingerprint = ['agent-session.js', 'compaction/compaction.js'].map(name => digest(readFileSync(file(name)))).join(':');
  if (!APPROVED.has(fingerprint)) throw Error('unreviewed native compaction source');
  const load = path => import(pathToFileURL(path).href);
  const { AgentSession } = await load(file('agent-session.js'));
  const sdk = await load(resolve(install, 'dist/index.js'));
  if (sdk.AgentSession !== AgentSession) throw Error('native SDK class identity mismatch');
  return { AgentSession, ...await load(file('compaction/compaction.js')),
    ...await load(file('compaction/utils.js')), ...await load(file('messages.js')), fingerprint };
}

export function installFence(runtime) {
  const { AgentSession, prepareCompaction, compact, serializeConversation, convertToLlm } = runtime;
  const prototype = AgentSession.prototype;
  if (prototype[MARKER]) {
    if (prototype[MARKER].version !== VERSION) throw Error('different fence already installed');
    return prototype[MARKER];
  }
  const sessions = new WeakMap();
  const originals = Object.fromEntries(['_runDefaultCompaction', '_runAutoCompaction', 'compact'].map(name => [name, prototype[name]]));
  if (Object.values(originals).some(method => typeof method !== 'function')) throw Error('native compaction methods missing');
  const key = (session, preparation, customInstructions) => digest(JSON.stringify({
    session: session.sessionManager.getSessionId(), provider: session.model?.provider,
    model: session.model?.id, thinkingLevel: session.thinkingLevel, customInstructions,
    messages: serializeConversation(convertToLlm(preparation.messagesToSummarize)),
    prefix: serializeConversation(convertToLlm(preparation.turnPrefixMessages)),
    previousSummary: preparation.previousSummary, settings: preparation.settings,
  }));
  prototype._runDefaultCompaction = async function (preparation, model, customInstructions, signal, reason) {
    const state = sessions.get(this.sessionManager);
    if (!state) return originals._runDefaultCompaction.apply(this, arguments);
    // Exact reviewed native default call, with a local dispatch observer. Never
    // replace agent.streamFunction or mutate any active request's options/signal.
    const inputKey = key(this, preparation, customInstructions);
    const request = await this._getSummarizationRequestAuth(model, signal);
    const stream = this.agent.streamFunction;
    let dispatched = false;
    const observedStream = typeof stream === 'function' ? async (...args) => {
      dispatched = true;
      const responseStream = await stream(...args);
      // Both fingerprinted native summarizers consume only .result(). Preserve
      // the original stream/response and reject aborted empty checkpoints locally.
      return { result: async () => {
        const response = await responseStream.result();
        if (response.stopReason === 'aborted') throw Error('Summarization failed: provider returned aborted response');
        return response;
      } };
    } : stream;
    try {
      return await compact(preparation, request.model, request.apiKey, request.headers,
        customInstructions, signal, request.thinkingLevel, observedStream,
        request.env, this.settingsManager.getRetrySettings(),
        this._summarizationRetryCallbacks({ source: 'compaction', reason }), undefined);
    } catch (error) {
      if (dispatched) state.failed = inputKey;
      throw error;
    }
  };
  prototype._runAutoCompaction = async function () {
    const state = sessions.get(this.sessionManager);
    if (state?.failed && this.model) {
      const preparation = prepareCompaction(this.sessionManager.getBranch(), this.settingsManager.getCompactionSettings(this.model));
      if (preparation && key(this, preparation, undefined) === state.failed) return false;
    }
    const leaf = this.sessionManager.getLeafId();
    const result = await originals._runAutoCompaction.apply(this, arguments);
    if (state && this.sessionManager.getLeafId() !== leaf && this.sessionManager.getLeafEntry()?.type === 'compaction') state.failed = undefined;
    return result;
  };
  prototype.compact = async function () {
    const result = await originals.compact.apply(this, arguments);
    const state = sessions.get(this.sessionManager);
    if (state) state.failed = undefined;
    return result;
  };
  const controller = { version: VERSION, originals, enable: manager => {
    if (!sessions.has(manager)) sessions.set(manager, {});
  }, disable: manager => sessions.delete(manager) };
  Object.defineProperty(prototype, MARKER, { value: controller });
  return controller;
}
