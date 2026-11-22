import assert from 'node:assert/strict';
import {fork} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdtemp, rm, writeFile, symlink, chmod} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {setTimeout as sleep} from 'node:timers/promises';
import {SharedTokenAdmission, createOpenAITokenFetch, retryDelayMs, withOpenAIAPIAdmission} from './admission.mjs';

const baseline = process.env.OPENAI_ADMISSION_BASELINE === '1';
const worker = new URL('./worker.fixture.mjs', import.meta.url);
const exactBody = tokens => {
  const payload = {model: 'gpt-6.2-astra', input: '', max_output_tokens: 16,
    reasoning: {effort: 'max'}, stream: true};
  payload.input = 'x'.repeat(tokens - 16 - Buffer.byteLength(JSON.stringify(payload)));
  return JSON.stringify(payload);
};

export async function workers(config, count, respond, prepare = () => {}) {
  const events = [];
  const children = [];
  let ready = 0;
  try {
    await Promise.all(Array.from({length: count}, (_, index) => new Promise((resolve, reject) => {
      const child = fork(worker, [JSON.stringify({...config, baseline})], {
        execArgv: [], env: {PATH: process.env.PATH, HOME: config.home, NODE_NO_WARNINGS: '1'},
        stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
      });
      children.push(child);
      let error = '';
      child.stderr.on('data', data => {error += data;});
      child.on('message', event => {
        if (event.type === 'ready') {
          if (++ready === count) Promise.resolve().then(prepare).then(
            () => children.forEach(peer => peer.send({type: 'start'})), reject);
        } else if (event.type === 'egress') {
          const record = {...event, worker: index, at: Date.now()};
          events.push(record);
          Promise.resolve(respond(record, events)).then(
            reply => {if (reply !== null) child.send({id: event.id, ...reply});}, reject);
        } else if (event.type === 'failure') reject(new Error(event.error));
      });
      child.on('error', reject);
      child.on('exit', code => code === 0 ? resolve() : reject(new Error(`child ${code}: ${error}`)));
    })));
    return events;
  } finally {
    for (const child of children) if (child.exitCode === null) child.kill();
  }
}

test('4M org budget: independent Pi workers cannot spend the same 50K remaining', {timeout: 60000}, async () => {
  const home = await mkdtemp(join(tmpdir(), 'openai-admission-test-'));
  const policy = {path: join(home, 'budget.sqlite'), scope: 'synthetic-org/astra',
    limit: 4000000, windowMs: 1400, pollMs: 20};
  let expires;
  try {
    const events = await workers({policy, home, body: exactBody(106848)}, 2,
      () => ({status: 200, body: 'synthetic complete'}), () => {
        if (!baseline) {
          const seed = new SharedTokenAdmission(policy);
          try {
            seed.observe({status: 200, headers: {'x-ratelimit-remaining-tokens': '50000'}});
            expires = seed.row().remote_until;
          } finally {seed.close();}
        } else expires = Date.now() + policy.windowMs;
      });
    assert.equal(events.length, 2);
    assert.ok(events.every(event => event.at >= expires - 5),
      `TPM collision: ${events.filter(event => event.at < expires - 5).length} workers dispatched into 50K remaining`);
    const hash = createHash('sha256').update(exactBody(106848)).digest('hex');
    assert.ok(events.every(event => event.hash === hash && event.model === 'gpt-6.2-astra'
      && event.effort === 'max' && event.headers.authorization === 'Bearer synthetic-only'));
  } finally {await rm(home, {recursive: true, force: true});}
});

export async function fixture(action, extra = {}) {
  const home = await mkdtemp(join(tmpdir(), 'openai-admission-test-'));
  const policy = {path: join(home, 'budget.sqlite'), scope: 'synthetic-org/astra',
    limit: 4000000, windowMs: 2000, pollMs: 20, ...extra};
  const admission = new SharedTokenAdmission(policy);
  try {return await action(admission, policy, home);}
  finally {admission.close(); await rm(home, {recursive: true, force: true});}
}

test('739/904ms native rejections share a persistent not-before, same request and model', {timeout: 60000}, async () => {
  await fixture(async (_admission, policy, home) => {
    const attempts = new Map();
    const body = exactBody(1024);
    const pending = [];
    const events = await workers({policy, home, body}, 2, event => {
      const attempt = (attempts.get(event.worker) ?? 0) + 1;
      attempts.set(event.worker, attempt);
      if (attempt > 1) return {status: 200, body: 'synthetic complete'};
      // Both initial sends must be in flight before either receives its 429.
      // Otherwise a late first send can legitimately follow an earlier retry.
      return new Promise(resolve => {
        pending.push(() => resolve({status: 429,
          headers: {'retry-after-ms': String(event.worker ? 904 : 739)},
          body: JSON.stringify({error: {code: 'rate_limit_exceeded',
            message: 'TPM: Limit 4000000, Used 0. Please try again in 739ms.'}})}));
        if (pending.length === 2) pending.forEach(reply => reply());
      });
    });
    assert.equal(events.length, 4);
    const rejects = events.slice(0, 2);
    const deadline = Math.max(...rejects.map(event => event.at + 1000));
    assert.ok(events.slice(2).every(event => event.at >= deadline));
    assert.equal(new Set(events.map(event => event.hash)).size, 1);
    assert.ok(events.every(event => event.model === 'gpt-6.2-astra' && event.effort === 'max'));
  });
});

test('six independent workers respect one rolling token budget', {timeout: 60000}, async () => {
  await fixture(async (_admission, policy, home) => {
    const events = await workers({policy, home, body: exactBody(240)}, 6,
      () => ({status: 200, body: 'synthetic complete'}));
    assert.equal(events.length, 6);
    for (const event of events) {
      // The fake provider's 300ms window is shorter than admission's 350ms,
      // leaving an explicit 50ms IPC transport margin, not a vendor claim.
      const count = events.filter(other => other.at <= event.at && other.at > event.at - 300).length;
      assert.ok(count * 240 <= 600, `cross-process overspend: ${count * 240}`);
    }
  }, {limit: 600, windowMs: 350});
});

test('in-flight sends cannot expire before delayed headers arrive', async () => {
  await fixture(async admission => {
    let release;
    let sent;
    const started = new Promise(resolve => {sent = resolve;});
    const held = new Promise(resolve => {release = resolve;});
    const response = new Response('first');
    let sends = 0;
    const fetch = createOpenAITokenFetch({admission, models: ['gpt-6.2-astra'], fetch: async () => {
      if (++sends === 1) {sent(); return held;}
      return new Response('second');
    }});
    const first = fetch('https://api.openai.com/v1/responses', {method: 'POST', body: exactBody(1024)});
    await started;
    const second = fetch('https://api.openai.com/v1/responses', {method: 'POST', body: exactBody(1024)});
    try {
      await sleep(180);
      assert.equal(sends, 1, 'a still-in-flight send lost its budget after one window');
      const replied = Date.now();
      release(response);
      assert.equal(await first, response);
      await second;
      assert.ok(Date.now() - replied >= 100);
      assert.equal(sends, 2);
    } finally {
      release(response);
      await Promise.allSettled([first, second]);
    }
  }, {limit: 1024, windowMs: 100, pollMs: 10});
});

test('a crashed sender keeps uncertain debt for a full window after discovery', async () => {
  await fixture(async (admission, policy, home) => {
    const events = await workers({policy, home, body: exactBody(1024), crashAfterSend: true}, 1, () => null);
    assert.equal(events.length, 1);
    await sleep(180);
    const discovered = Date.now();
    await admission.acquire(1024);
    assert.ok(Date.now() - discovered >= policy.windowMs - 5, 'dead worker debt was refunded or backdated');
  }, {limit: 1024, windowMs: 100, pollMs: 10});
});

test('Retry-After parsing honors the longest header/body hint, never caps long holds', () => {
  const now = Date.parse('2026-11-19T16:00:00Z');
  assert.equal(retryDelayMs(new Headers({'retry-after-ms': '904'}), 'try again in 739ms', now), 1000);
  assert.equal(retryDelayMs(new Headers({'retry-after': '120', 'retry-after-ms': '904'}), '', now), 120000);
  assert.equal(retryDelayMs(new Headers({'retry-after': 'Thu, 19 Nov 2026 16:02:00 GMT'}), '', now), 120000);
  assert.equal(retryDelayMs(new Headers({'x-ratelimit-reset-tokens': '1m2.5s'}), '', now), 62500);
});

test('queued cancellation sends nothing and leaves no speculative token debit', async () => {
  await fixture(async admission => {
    admission.observe({status: 429, headers: new Headers({'retry-after': '60'}),
      error: {code: 'rate_limit_exceeded', message: 'TPM'}});
    let sends = 0;
    const fetch = createOpenAITokenFetch({admission, models: ['gpt-6.2-astra'],
      fetch: async () => {sends++; return new Response('unexpected');}});
    const controller = new AbortController();
    const pending = fetch('https://api.openai.com/v1/responses', {method: 'POST',
      body: exactBody(1024), signal: controller.signal});
    controller.abort();
    await assert.rejects(pending, {name: 'AbortError'});
    assert.equal(sends, 0);
    assert.equal(admission.db.prepare('SELECT count(*) AS n FROM debits').get().n, 0);
  });
});

test('success and shorter parallel 429 cannot erase the longest shared hold', async () => {
  await fixture(async (admission, policy) => {
    const peer = new SharedTokenAdmission(policy);
    try {
      admission.observe({status: 429, headers: {'retry-after': '120'},
        error: {code: 'rate_limit_exceeded', message: 'TPM'}});
      const deadline = admission.row().blocked_until;
      peer.observe({status: 429, headers: {'retry-after-ms': '739'},
        error: {code: 'rate_limit_exceeded', message: 'TPM'}});
      peer.observe({status: 200, headers: {'x-ratelimit-remaining-tokens': '4000000'}});
      assert.equal(peer.row().blocked_until, deadline);
    } finally {peer.close();}
  });
});

test('conflicting worker policy refuses; distinct orgs have distinct buckets', async () => {
  await fixture(async (admission, policy) => {
    assert.throws(() => new SharedTokenAdmission({...policy, limit: policy.limit + 1}), /Conflicting/);
    const other = new SharedTokenAdmission({...policy, scope: 'another-org/astra'});
    try {
      admission.observe({status: 429, headers: {'retry-after': '120'},
        error: {code: 'rate_limit_exceeded', message: 'TPM'}});
      await other.acquire(100);
    } finally {other.close();}
  });
});

test('stream response passes by identity; uncertain sends and partial streams never replay', async () => {
  await fixture(async admission => {
    const response = new Response('data: synthetic\n\n', {headers: {'content-type': 'text/event-stream'}});
    let sends = 0;
    const fetch = createOpenAITokenFetch({admission, models: ['gpt-6.2-astra'], fetch: async () => {sends++; return response;}});
    assert.equal(await fetch('https://api.openai.com/v1/responses', {method: 'POST', body: exactBody(1024)}), response);
    assert.equal(sends, 1);
    let uncertain = 0;
    const failing = createOpenAITokenFetch({admission, models: ['gpt-6.2-astra'], fetch: async () => {
      uncertain++; throw new TypeError('synthetic disconnected before headers');
    }});
    await assert.rejects(failing('https://api.openai.com/v1/responses', {method: 'POST', body: exactBody(1024)}), /disconnected/);
    assert.equal(uncertain, 1);
  });
});

for (const cancel of [false, true]) test(`worker startup contention ${cancel ? 'cancels' : 'waits'} instead of losing admission`, async () => {
  await fixture(async (admission, policy) => {
    const controller = new AbortController();
    admission.db.exec('BEGIN IMMEDIATE');
    let locked = true;
    let opened;
    const timer = setTimeout(() => {
      admission.db.exec('ROLLBACK');
      locked = false;
      if (cancel) controller.abort();
    }, 80);
    try {
      const pending = SharedTokenAdmission.open(policy, controller.signal);
      if (cancel) await assert.rejects(pending, {name: 'AbortError'});
      else {
        opened = await pending;
        await opened.acquire(100);
        assert.equal(opened.row().configured_limit, policy.limit);
      }
    } finally {
      clearTimeout(timer);
      if (locked) admission.db.exec('ROLLBACK');
      opened?.close();
    }
  });
});

for (const cancel of [false, true]) test(`post-send DB contention ${cancel ? 'cancels' : 'delivers the same response'} without replay`, async () => {
  await fixture(async (admission, policy) => {
    const peer = new SharedTokenAdmission(policy);
    const controller = new AbortController();
    const response = new Response('data: synthetic\n\n');
    let sends = 0;
    let locked = false;
    let timer;
    try {
      const fetch = createOpenAITokenFetch({admission, models: ['gpt-6.2-astra'], fetch: async () => {
        sends++;
        peer.db.exec('BEGIN IMMEDIATE');
        locked = true;
        timer = setTimeout(() => {
          peer.db.exec('ROLLBACK');
          locked = false;
          if (cancel) controller.abort();
        }, 50);
        return response;
      }});
      const pending = fetch('https://api.openai.com/v1/responses', {
        method: 'POST', body: exactBody(1024), signal: controller.signal});
      if (cancel) await assert.rejects(pending, {name: 'AbortError'});
      else assert.equal(await pending, response);
      assert.equal(sends, 1);
      assert.equal(admission.db.prepare('SELECT count(*) AS n FROM debits').get().n, 1);
    } finally {
      clearTimeout(timer);
      if (locked) peer.db.exec('ROLLBACK');
      peer.close();
    }
  });
});

for (const [name, status, error, headers] of [
  ['quota exhausted', 429, {code: 'insufficient_quota'}, {}],
  ['unknown 429', 429, {message: 'unknown'}, {}],
  ['explicit no retry', 429, {code: 'rate_limit_exceeded'}, {'x-should-retry': 'false'}],
  ['authentication', 401, {code: 'invalid_api_key'}, {}],
  ['server error', 500, {code: 'server_error'}, {}],
]) test(`${name} stays native and is never replayed`, async () => {
  await fixture(async admission => {
    let sends = 0;
    const response = new Response(JSON.stringify({error}), {status, headers});
    const fetch = createOpenAITokenFetch({admission, models: ['gpt-6.2-astra'], fetch: async () => {sends++; return response;}});
    assert.equal(await fetch('https://api.openai.com/v1/responses', {method: 'POST', body: exactBody(1024)}), response);
    assert.equal(sends, 1);
  });
});

for (const remote of [
  {input: [{type: 'input_image', image_url: 'https://example.invalid/image'}]},
  {previous_response_id: 'opaque'}, {conversation: 'opaque'}, {prompt: {id: 'opaque'}},
  {tools: [{type: 'web_search'}]},
]) test('opaque server-side input refuses before any egress', async () => {
  await fixture(async admission => {
    let sends = 0;
    const fetch = createOpenAITokenFetch({admission, models: ['gpt-6.2-astra'], fetch: () => {sends++;}});
    await assert.rejects(fetch('https://api.openai.com/v1/responses', {method: 'POST',
      body: JSON.stringify({model: 'gpt-6.2-astra', ...remote})}), /no token bound/);
    assert.equal(sends, 0);
  });
});

test('oversized request and lower provider capacity refuse without altering a request', async () => {
  await fixture(async admission => {
    admission.observe({status: 200, headers: {'x-ratelimit-limit-tokens': '100'}});
    await assert.rejects(admission.acquire(101), /exceeds/);
  });
});

test('provider wrapper preserves all other providers/models and double-wraps only once', async () => {
  await fixture(async admission => {
    const native = (_model, _context, options) => options;
    const provider = {id: 'openai-api', stream: native, streamSimple: native, auth: {same: true}};
    const options = {apiKey: 'synthetic', reasoning: 'max', fetch: async () => new Response('fixture')};
    const wrapped = withOpenAIAPIAdmission(provider, {admission, models: ['gpt-6.2-astra']});
    assert.equal(wrapped.auth, provider.auth);
    assert.equal(wrapped.streamSimple({provider: 'openai-codex'}, {}, options), options);
    assert.throws(() => withOpenAIAPIAdmission({...provider, id: 'openai-codex'}, {admission}), /direct API/);
    const twice = withOpenAIAPIAdmission(wrapped, {admission, models: ['gpt-6.2-astra']});
    const model = {provider: 'openai-api', id: 'gpt-6.2-astra', api: 'openai-responses',
      baseUrl: 'https://api.openai.com/v1', maxTokens: 16};
    const actual = twice.streamSimple(model, {}, options);
    assert.equal(actual.apiKey, options.apiKey);
    assert.equal(actual.reasoning, options.reasoning);
    await actual.fetch('https://api.openai.com/v1/responses', {method: 'POST', body: exactBody(1024)});
    assert.equal(admission.db.prepare('SELECT count(*) AS n FROM debits').get().n, 1);
  });
});

test('Pi extension fails closed for Astra if configuration is absent, leaving other models alone', async () => {
  const {default: install} = await import('./pi-extension.ts');
  const events = {};
  const native = () => 'other-model';
  let provider = {id: 'openai-api', stream: native, streamSimple: native};
  const pi = {on: (name, handler) => {events[name] = handler;}, registerProvider: value => {provider = value;}};
  install(pi);
  const previous = process.env.PI_OPENAI_TOKEN_ADMISSION_CONFIG;
  delete process.env.PI_OPENAI_TOKEN_ADMISSION_CONFIG;
  try {
    await assert.rejects(events.session_start({}, {modelRegistry: {getProvider: () => provider}}), /Explicit/);
    assert.throws(() => provider.streamSimple({id: 'gpt-6.2-astra'}), /unavailable/);
    assert.equal(provider.streamSimple({id: 'gpt-6.1-sol'}), 'other-model');
  } finally {
    if (previous !== undefined) process.env.PI_OPENAI_TOKEN_ADMISSION_CONFIG = previous;
  }
});

test('private database protections reject symlinks and permissive storage', async () => {
  const home = await mkdtemp(join(tmpdir(), 'openai-admission-test-'));
  try {
    const target = join(home, 'target');
    const path = join(home, 'link');
    await writeFile(target, '');
    await symlink(target, path);
    assert.throws(() => new SharedTokenAdmission({path, scope: 'test', limit: 100}), /private regular/);
    await chmod(home, 0o755);
    assert.throws(() => new SharedTokenAdmission({path: join(home, 'another'), scope: 'test', limit: 100}), /private and owner/);
  } finally {await rm(home, {recursive: true, force: true});}
});
