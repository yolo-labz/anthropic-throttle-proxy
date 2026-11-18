#!/usr/bin/env node
// Offline acceptance for an installed Pi artifact. Only loopback mock HTTP is used.
import assert from 'node:assert/strict';
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const install = resolve(process.argv[2]);
const expected = process.argv[3];
assert(['survives', 'reproduces'].includes(expected), 'usage: script INSTALL survives|reproduces');
const load = (path) => import(pathToFileURL(`${install}/${path}`).href);
const { stream } = await load('node_modules/@earendil-works/pi-ai/dist/api/anthropic-messages.js');
const { configureHttpDispatcher } = await load('dist/core/http-dispatcher.js');
const undici = await load('node_modules/undici/index.js');
for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']) delete process.env[key];
const idleMs = 200;
configureHttpDispatcher(idleMs);
const timers = new Set();
const server = http.createServer((req, res) => {
  req.resume();
  res.writeHead(200, { 'content-type': 'text/event-stream' });
  const emit = (event) => res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  emit({ type: 'message_start', message: { id: 'mock', model: 'claude-mock', role: 'assistant', usage: { input_tokens: 1, output_tokens: 0 } } });
  if (req.url.includes('idle')) return;
  emit({ type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } });
  let ticks = 0;
  const timer = setInterval(() => {
    if (++ticks < 12) return emit({ type: 'ping' });
    clearInterval(timer);
    timers.delete(timer);
    emit({ type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'mock-complete' } });
    emit({ type: 'content_block_stop', index: 0 });
    emit({ type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 1 } });
    emit({ type: 'message_stop' });
    res.end();
  }, 50);
  timers.add(timer);
  res.on('close', () => { clearInterval(timer); timers.delete(timer); });
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const baseUrl = `http://127.0.0.1:${server.address().port}`;
const model = { id: 'claude-mock', name: 'Mock', api: 'anthropic-messages', provider: 'anthropic', baseUrl,
  reasoning: false, input: ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100000, maxTokens: 16 };
const context = { messages: [{ role: 'user', content: 'offline mock', timestamp: 1 }] };
const start = performance.now();
try {
  const result = await stream(model, context, { apiKey: 'offline-placeholder', timeoutMs: idleMs,
    gatewayPreEgressTrust: {}, maxRetries: 0 }).result();
  const elapsedMs = Math.round(performance.now() - start);
  if (expected === 'survives') {
    assert.equal(result.stopReason, 'stop', JSON.stringify({ stopReason: result.stopReason, error: result.errorMessage }));
    assert(elapsedMs > idleMs * 2, 'stream must span multiple idle windows');
  } else {
    assert.equal(result.stopReason, 'error', 'old artifact must reproduce internal deadline failure');
    assert(elapsedMs < idleMs * 2.5, 'old absolute deadline must fire before mock completion');
  }
  // A distinct real transport request must still stop when no body bytes arrive.
  const idleStart = performance.now();
  await assert.rejects(async () => { const response = await undici.fetch(`${baseUrl}/idle`); await response.text(); },
    (error) => error?.cause?.code === 'UND_ERR_BODY_TIMEOUT' || error?.code === 'UND_ERR_BODY_TIMEOUT');
  const idleElapsedMs = Math.round(performance.now() - idleStart);
  assert(idleElapsedMs >= idleMs && idleElapsedMs < 3000, 'true idle must remain bounded by its own transport timer');
  const caller = new AbortController();
  const cancel = setTimeout(() => caller.abort(), 75);
  let cancelled;
  try {
    cancelled = await stream(model, context, { apiKey: 'offline-placeholder', timeoutMs: idleMs,
      gatewayPreEgressTrust: {}, signal: caller.signal, maxRetries: 0 }).result();
    assert.equal(cancelled.stopReason, 'aborted', 'caller cancellation must remain effective');
  } finally { clearTimeout(cancel); }
  console.log(JSON.stringify({ install, expected, stopReason: result.stopReason, elapsedMs, idleElapsedMs,
    callerStopReason: cancelled.stopReason, passed: true }));
} finally {
  for (const timer of timers) clearInterval(timer);
  server.closeAllConnections();
  await new Promise((done) => server.close(done));
  await undici.getGlobalDispatcher().close();
}
