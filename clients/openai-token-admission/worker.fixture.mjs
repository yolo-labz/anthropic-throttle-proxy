// Isolated child process: synthetic credential, IPC upstream, no network or auth store.
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(process.argv[2]);
globalThis.fetch = () => {throw new Error('Synthetic worker forbids real network egress');};
let sequence = 0;
let controller;
let timer;
const replies = new Map();
let start;
const ready = new Promise(resolve => {start = resolve;});
process.on('message', message => {
  if (message.type === 'start') {start(); return;}
  const resolve = replies.get(message.id);
  replies.delete(message.id);
  if (config.abortAfter && message.status === 429) {
    timer = setTimeout(() => controller.abort(), config.abortAfter);
  }
  resolve?.(new Response(message.body, {status: message.status, headers: message.headers}));
});
const upstream = async (input, init) => {
  const id = ++sequence;
  const body = init.body;
  const payload = JSON.parse(body);
  const response = new Promise(resolve => replies.set(id, resolve));
  process.send({type: 'egress', id, url: String(input),
    hash: createHash('sha256').update(body).digest('hex'),
    model: payload.model, effort: payload.reasoning?.effort,
    headers: Object.fromEntries(new Headers(init.headers))},
    config.crashAfterSend ? () => process.exit(0) : undefined);
  return response;
};
try {
  let fetch = upstream;
  let admission;
  if (!config.baseline) {
    const module = await import('./admission.mjs');
    admission = await module.SharedTokenAdmission.open(config.policy);
    fetch = module.createOpenAITokenFetch({admission, fetch: upstream,
      models: ['gpt-6.2-astra'], outputBound: 16});
  }
  const native = config.nativeProvider ? await import(pathToFileURL(config.nativeProvider)) : null;
  process.send({type: 'ready'});
  await ready;
  if (native) {
    const model = {id: 'gpt-6.2-astra', name: 'Astra fixture', provider: 'openai-api',
      api: 'openai-responses', baseUrl: 'https://api.openai.com/v1', reasoning: true,
      input: ['text'], contextWindow: 1050000, maxTokens: 16,
      cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}};
    const {withOpenAIAPIAdmission} = await import('./admission.mjs');
    const provider = withOpenAIAPIAdmission({id: 'openai-api', ...native},
      {admission, models: ['gpt-6.2-astra']});
    controller = new AbortController();
    const stream = provider.streamSimple(model,
      {messages: [{role: 'user', content: 'synthetic', timestamp: 1}]},
      {apiKey: 'synthetic-only', maxTokens: 16, reasoning: 'high', maxRetries: 0,
        fetch: upstream, signal: controller.signal});
    const result = await stream.result();
    clearTimeout(timer);
    if (result.stopReason !== (config.expectedTerminal ?? 'stop')) {
      throw new Error(`native terminal: ${result.stopReason}: ${result.errorMessage}`);
    }
    process.send({type: 'done', stopReason: result.stopReason, text: result.content});
  } else {
    const response = await fetch('https://api.openai.com/v1/responses', {
      method: 'POST', headers: {authorization: 'Bearer synthetic-only', 'content-type': 'application/json'},
      body: config.body,
    });
    process.send({type: 'done', status: response.status, text: await response.text()});
  }
  admission?.close();
  process.disconnect();
} catch (error) {
  process.send({type: 'failure', error: error.stack});
  process.exitCode = 1;
  process.disconnect();
}
