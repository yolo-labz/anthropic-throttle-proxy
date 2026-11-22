// Explicit installed-core qualification includes the source suite, without skips.
import assert from 'node:assert/strict';
import test from 'node:test';
import {fixture, workers} from './admission.test.mjs';

const nativeProvider = process.env.PI_AI_OPENAI_RESPONSES;
if (!nativeProvider) throw new Error('Set PI_AI_OPENAI_RESPONSES to qualify the exact installed core');
test('native installed Pi Responses transport retains its terminal SSE through a 429',
  {timeout: 60000}, async () => {
  await fixture(async (_admission, policy, home) => {
    const terminal = {type: 'response.completed', sequence_number: 0,
      response: {id: 'synthetic-response', status: 'completed', usage: {
        input_tokens: 1, output_tokens: 1, total_tokens: 2, input_tokens_details: {cached_tokens: 0}}}};
    const events = await workers({policy, home, nativeProvider}, 1, (_event, events) => events.length === 1
      ? {status: 429, headers: {'retry-after-ms': '739'},
        body: JSON.stringify({error: {code: 'rate_limit_exceeded', message: 'TPM rejected'}})}
      : {status: 200, headers: {'content-type': 'text/event-stream'}, body: `data: ${JSON.stringify(terminal)}\n\n`});
    assert.equal(events.length, 2);
    assert.equal(events[0].hash, events[1].hash);
    assert.ok(events[1].at - events[0].at >= 1000);
    assert.ok(events.every(event => event.model === 'gpt-6.2-astra'));
  });
});

test('native Pi never replays incomplete SSE or invents completion',
  {timeout: 60000}, async () => {
    await fixture(async (_admission, policy, home) => {
      const events = await workers({policy, home, nativeProvider, expectedTerminal: 'error'}, 1,
        () => ({status: 200, headers: {'content-type': 'text/event-stream'},
          body: 'data: {"type":"response.created","response":{"id":"synthetic"}}\n\n'}));
      assert.equal(events.length, 1);
    });
  });

test('native Pi cancellation remains aborted while waiting on a long Retry-After',
  {timeout: 60000}, async () => {
    await fixture(async (_admission, policy, home) => {
      const events = await workers({policy, home, nativeProvider, expectedTerminal: 'aborted', abortAfter: 100}, 1,
        () => ({status: 429, headers: {'retry-after': '120'},
          body: JSON.stringify({error: {code: 'rate_limit_exceeded', message: 'TPM'}})}));
      assert.equal(events.length, 1);
    });
  });

