import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, lstatSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import factory, { qualify, bootstrap } from './core-upgrade-guard.ts';

const now = 1000000;
const request = { schema: 1, nonce: 'a'.repeat(64), expires: now + 60000, pid: 123,
  session: '/original.jsonl', cwd: '/project', provider: 'claude-lane', model: 'claude-fable-5-2',
  effort: 'max', install: '/old' };
const actual = { ...request, idle: true, pending: false, editor: '' };
test('exact empty native idle state qualifies', () => assert.doesNotThrow(() => qualify(request, actual, now)));
for (const [key, value] of Object.entries({ idle: false, pending: true, editor: 'human draft', pid: 124,
  session: '/other.jsonl', cwd: '/other', provider: 'api', model: 'other', effort: 'high', install: '/other' })) {
  test(`refuses changed ${key}`, () => assert.throws(() => qualify(request, { ...actual, [key]: value }, now)));
}
for (const patch of [{ expires: now }, { expires: now + 300001 }, { expires: 'later' }, { nonce: '../bad' }, { schema: 2 }]) {
  test(`refuses invalid request ${JSON.stringify(patch)}`, () => assert.throws(() => qualify({ ...request, ...patch }, actual, now)));
}
test('unknown native flags are not idle evidence', () => {
  for (const patch of [{ idle: undefined }, { pending: undefined }, { editor: null }]) {
    assert.throws(() => qualify(request, { ...actual, ...patch }, now));
  }
});
test('recorded bootstrap role must match actual native role', () => {
  assert.throws(() => qualify({ ...request, role: 'review' }, { ...actual, role: 'pearson~' }, now));
  assert.doesNotThrow(() => qualify({ ...request, role: 'review' }, { ...actual, role: 'review' }, now));
});
test('native bootstrap rejects unknown flags and all positional inference input', () => {
  for (const flags of [['--api-key', 'do-not-copy'], ['--unknown', 'value'], ['human prompt'], ['--print'], ['--model']]) {
    const result = bootstrap(['/nix/store/node/bin/node', '/core/dist/cli.js', ...flags]);
    assert.equal(result.qualified, false);
    assert.equal(result.args, undefined);
  }
  assert.equal(bootstrap(['/nix/store/node/bin/node', '/core/dist/cli.js', '--no-skills', '--skill', '/skill', '--thinking', 'max']).qualified, true);
  assert.deepEqual(bootstrap(['/node', '/cli', '--append-system-prompt', '-c', '--model', 'old', '--continue']).replayArgs,
    ['--append-system-prompt', '-c']);
});

test('registered handler cannot shut down unsafe state and state command never quits', () => {
  const home = mkdtempSync(join(tmpdir(), 'core-guard-test-'));
  const previousHome = process.env.HOME;
  const previousInstall = process.env.PI_FLEET_INSTALL;
  const previousArgv = process.argv;
  process.argv = [process.execPath, '/old/dist/cli.js', '--name', 'review'];
  process.env.HOME = home;
  process.env.PI_FLEET_INSTALL = '/old';
  try {
    const directory = join(home, '.local/state/throttler-recovery-20261111/core-upgrades');
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const commands = {};
    let shutdowns = 0;
    const pi = { registerCommand: (name, definition) => { commands[name] = definition; }, getThinkingLevel: () => 'max' };
    const ctx = { cwd: '/project', model: { provider: 'claude-lane', id: 'claude-fable-5-2' }, hasUI: true,
      sessionManager: { getSessionFile: () => '/original.jsonl' }, isIdle: () => true, hasPendingMessages: () => false,
      ui: { getEditorText: () => '', notify: () => {} }, shutdown: () => { shutdowns++; } };
    factory(pi);
    const loadedPath = join(directory, `${process.pid}.loaded.json`);
    const firstLoad = JSON.parse(readFileSync(loadedPath, 'utf8'));
    factory(pi);
    const secondLoad = JSON.parse(readFileSync(loadedPath, 'utf8'));
    assert.notEqual(firstLoad.generation, secondLoad.generation);
    assert.equal(secondLoad.pid, process.pid);
    assert.equal(secondLoad.guardVersion, 'native-load-v1');
    assert.equal(lstatSync(loadedPath).mode & 0o777, 0o600);
    let sequence = 0;
    const invoke = (patch = {}, context = ctx, mode = 'ready', permissions = 0o600, failReceipt = false) => {
      const nonce = (++sequence).toString(16).padStart(64, '0');
      const body = { ...request, nonce, pid: process.pid, expires: Date.now() + 60000,
        expectedBootstrapHash: bootstrap(process.argv).hash, ...patch };
      writeFileSync(join(directory, `${nonce}.request.json`), JSON.stringify(body), { mode: permissions });
      if (failReceipt) mkdirSync(join(directory, `${nonce}.${mode}.json`));
      commands[`core-upgrade-${mode}`].handler(nonce, context);
      return nonce;
    };
    for (const context of [{ ...ctx, isIdle: () => false }, { ...ctx, hasPendingMessages: () => true },
      { ...ctx, ui: { ...ctx.ui, getEditorText: () => 'private human draft' } }]) {
      const nonce = invoke({}, context);
      const path = join(directory, `${nonce}.refused.json`);
      const refusal = JSON.parse(readFileSync(path, 'utf8'));
      assert.equal(refusal.nonce, nonce);
      assert.equal(refusal.command, 'core-upgrade-ready');
      assert.equal(refusal.reason, 'native-not-idle');
      assert.equal(refusal.actual.pid, process.pid);
      assert.equal(refusal.actual.model, ctx.model.id);
      assert.equal(lstatSync(path).mode & 0o777, 0o600);
      assert.equal(JSON.stringify(refusal).includes('private human draft'), false);
      assert.equal('editor' in refusal.actual, false);
    }
    for (const patch of [{ pid: -1 }, { model: 'wrong' }, { expires: 1 }, { nonce: 'b'.repeat(64) }]) invoke(patch);
    const unavailableNonce = invoke({}, { ...ctx, isIdle: () => { throw Error('private native failure'); } });
    const unavailable = JSON.parse(readFileSync(join(directory, `${unavailableNonce}.refused.json`), 'utf8'));
    assert.equal(unavailable.reason, 'native-observation-unavailable');
    assert.equal(unavailable.identityMismatchKey, null);
    assert.equal(unavailable.actual.pid, process.pid);
    assert.equal(unavailable.actual.install, '/old');
    assert.equal(unavailable.actual.idle, null);
    assert.equal(unavailable.actual.pending, null);
    assert.equal(unavailable.actual.editorBytes, null);
    assert.equal(JSON.stringify(unavailable).includes('private native failure'), false);
    const mismatchNonce = invoke({ model: 'private unexpected model' });
    const mismatch = JSON.parse(readFileSync(join(directory, `${mismatchNonce}.refused.json`), 'utf8'));
    assert.equal(mismatch.reason, 'identity-mismatch');
    assert.equal(mismatch.identityMismatchKey, 'model');
    assert.equal(JSON.stringify(mismatch).includes('private unexpected model'), false);
    const unsafeNonce = invoke({}, ctx, 'ready', 0o644);
    assert.equal(existsSync(join(directory, `${unsafeNonce}.refused.json`)), false);
    const failedNonce = invoke({}, ctx, 'ready', 0o600, true);
    const failure = JSON.parse(readFileSync(join(directory, `${failedNonce}.refused.json`), 'utf8'));
    assert.equal(failure.reason, 'internal-error');
    assert.equal(JSON.stringify(failure).includes(directory), false);
    invoke({}, ctx, 'state');
    assert.equal(shutdowns, 0);
    invoke();
    assert.equal(shutdowns, 1);
  } finally {
    if (previousHome === undefined) delete process.env.HOME; else process.env.HOME = previousHome;
    if (previousInstall === undefined) delete process.env.PI_FLEET_INSTALL; else process.env.PI_FLEET_INSTALL = previousInstall;
    process.argv = previousArgv;
    rmSync(home, { recursive: true, force: true });
  }
});
