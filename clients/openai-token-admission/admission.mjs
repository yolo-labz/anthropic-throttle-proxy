// Direct API admission only. SQLite arbitrates separate Pi processes on this host.
import {DatabaseSync} from 'node:sqlite';
import {createHash} from 'node:crypto';
import {closeSync, lstatSync, mkdirSync, openSync, readFileSync} from 'node:fs';
import {dirname, isAbsolute} from 'node:path';
import {setTimeout as sleep} from 'node:timers/promises';

const positive = value => Number.isSafeInteger(value) && value > 0;
const busy = error => /database is (locked|busy)/.test(error.message);
const processOwner = pid => {
  const stat = readFileSync(`/proc/${pid}/stat`, 'utf8');
  return `${pid}:${stat.slice(stat.lastIndexOf(') ') + 2).trim().split(/\s+/)[19]}`;
};
const ownerAlive = owner => {
  if (!/^\d+:\d+$/.test(owner ?? '')) return true;
  try {return processOwner(owner.split(':')[0]) === owner;}
  catch (error) {return !['ENOENT', 'ESRCH'].includes(error.code);}
};

export function durationMs(value) {
  if (typeof value !== 'string' || !/^(?:\d+(?:\.\d+)?(?:ms|s|m|h))+$/.test(value)) return null;
  let result = 0;
  for (const match of value.matchAll(/(\d+(?:\.\d+)?)(ms|s|m|h)/g)) {
    result += Number(match[1]) * {ms: 1, s: 1000, m: 60000, h: 3600000}[match[2]];
  }
  return Number.isSafeInteger(Math.ceil(result)) ? Math.ceil(result) : null;
}

export function retryDelayMs(headers, message = '', now = Date.now()) {
  const delays = [1000];
  const numeric = (value, factor) => {
    if (value !== null && /^\d+(?:\.\d+)?$/.test(value)) delays.push(Number(value) * factor);
  };
  numeric(headers.get('retry-after-ms'), 1);
  const after = headers.get('retry-after');
  numeric(after, 1000);
  if (after && !/^\d+(?:\.\d+)?$/.test(after) && Number.isFinite(Date.parse(after))) {
    delays.push(Date.parse(after) - now);
  }
  const reset = durationMs(headers.get('x-ratelimit-reset-tokens'));
  if (reset !== null) delays.push(reset);
  const hint = /try again in\s+(\d+(?:\.\d+)?(?:ms|s|m|h))/i.exec(message);
  if (hint) delays.push(durationMs(hint[1].toLowerCase()));
  return Math.ceil(Math.max(...delays.filter(value => Number.isFinite(value) && value >= 0)));
}

export class SharedTokenAdmission {
  static async open(policy, signal) {
    for (;;) {
      signal?.throwIfAborted();
      try {return new SharedTokenAdmission(policy);}
      catch (error) {
        if (!busy(error)) throw error;
        await sleep(25, undefined, {signal});
      }
    }
  }

  constructor({path, scope, limit, windowMs = 60000, pollMs = 1000, clock = Date.now}) {
    if (!isAbsolute(path ?? '') || typeof scope !== 'string' || !scope || scope.length > 512
      || !positive(limit) || !positive(windowMs) || !positive(pollMs) || typeof clock !== 'function') {
      throw new Error('Explicit shared OpenAI org/rate-group policy required');
    }
    mkdirSync(dirname(path), {recursive: true, mode: 0o700});
    const parent = lstatSync(dirname(path));
    if (!parent.isDirectory() || parent.uid !== process.getuid() || (parent.mode & 0o077)) {
      throw new Error('OpenAI admission directory must be private and owner-controlled');
    }
    try {closeSync(openSync(path, 'wx', 0o600));} catch (error) {if (error.code !== 'EEXIST') throw error;}
    const file = lstatSync(path);
    if (!file.isFile() || file.uid !== process.getuid() || (file.mode & 0o077)) {
      throw new Error('OpenAI admission database must be a private regular file');
    }
    this.owner = processOwner(process.pid);
    this.key = createHash('sha256').update(scope).digest('hex');
    this.clock = clock;
    this.pollMs = pollMs;
    this.db = new DatabaseSync(path);
    try {
      this.db.exec(`PRAGMA busy_timeout=25;
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS buckets (
          scope TEXT PRIMARY KEY, configured_limit INTEGER NOT NULL, window_ms INTEGER NOT NULL,
          cap INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0,
          remote_used INTEGER NOT NULL DEFAULT 0, remote_until REAL NOT NULL DEFAULT 0,
          last_now REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS debits (
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, at REAL NOT NULL, tokens INTEGER NOT NULL, owner TEXT);
        CREATE INDEX IF NOT EXISTS debits_scope_time ON debits(scope, at);`);
      this.transaction(() => {
        if (!this.db.prepare('PRAGMA table_info(debits)').all().some(column => column.name === 'owner')) {
          this.db.exec('ALTER TABLE debits ADD COLUMN owner TEXT');
        }
        this.db.prepare('INSERT OR IGNORE INTO buckets(scope, configured_limit, window_ms, cap) VALUES(?,?,?,?)')
          .run(this.key, limit, windowMs, limit);
        const row = this.row();
        if (row.configured_limit !== limit || row.window_ms !== windowMs) {
          throw new Error('Conflicting OpenAI admission policy for the same org/rate group');
        }
      });
    } catch (error) {this.db.close(); throw error;}
  }

  row() {return this.db.prepare('SELECT * FROM buckets WHERE scope=?').get(this.key);}

  transaction(action) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = action();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {this.db.exec('ROLLBACK'); throw error;}
  }

  now(row) {
    const time = this.clock();
    if (!Number.isSafeInteger(time) || time < 0) throw new Error('Invalid admission clock');
    // A backwards wall clock cannot expire another worker's reservations early.
    return Math.max(time, row.last_now);
  }

  async acquire(tokens, signal) {
    if (!positive(tokens)) throw new Error('Positive integer token bound required');
    for (;;) {
      signal?.throwIfAborted();
      let wait;
      let grant;
      try {
        wait = this.transaction(() => {
          const row = this.row();
          const now = this.now(row);
          if (tokens > row.cap) throw new Error('Request token bound exceeds the org TPM capacity; payload preserved');
          // A stalled sender must retain budget until headers/error, even when
          // scheduling or transport takes longer than a quota window.
          const owners = new Map();
          for (const debit of this.db.prepare('SELECT id,owner FROM debits WHERE scope=? AND at<0').all(this.key)) {
            if (!owners.has(debit.owner)) owners.set(debit.owner, ownerAlive(debit.owner));
            if (!owners.get(debit.owner)) {
              // A dead worker's uncertain send retains one FULL window from
              // discovery; never refund it or backdate to its old grant.
              this.db.prepare('UPDATE debits SET at=? WHERE id=?').run(now, debit.id);
            }
          }
          this.db.prepare('DELETE FROM debits WHERE scope=? AND at>=0 AND at<=?').run(this.key, now - row.window_ms);
          const debits = this.db.prepare('SELECT at,tokens FROM debits WHERE scope=? ORDER BY at,id').all(this.key);
          let used = debits.reduce((sum, debit) => sum + debit.tokens, 0);
          if (row.remote_until > now) used += row.remote_used;
          if (now >= row.blocked_until && used + tokens <= row.cap) {
            this.db.prepare('UPDATE buckets SET last_now=? WHERE scope=?').run(now, this.key);
            grant = Number(this.db.prepare('INSERT INTO debits(scope,at,tokens,owner) VALUES(?,-1,?,?)')
              .run(this.key, tokens, this.owner).lastInsertRowid);
            return 0;
          }
          const expiries = debits.filter(debit => debit.at >= 0).map(debit => debit.at + row.window_ms);
          if (row.remote_until > now) expiries.push(row.remote_until);
          const budgetWait = used + tokens > row.cap ? Math.min(...expiries) - now : 0;
          return Math.max(1, row.blocked_until - now, budgetWait);
        });
      } catch (error) {
        if (!busy(error)) throw error;
        wait = 25;
      }
      if (!wait) return grant;
      // Release the DB lock before waiting; recheck other workers' new holds.
      await sleep(Math.min(wait + 1, this.pollMs), undefined, {signal});
    }
  }

  async settle(grant) {
    for (;;) {
      try {
        this.transaction(() => {
          const now = this.now(this.row());
          this.db.prepare('UPDATE debits SET at=? WHERE id=? AND scope=? AND at<0').run(now, grant, this.key);
          this.db.prepare('UPDATE buckets SET last_now=? WHERE scope=?').run(now, this.key);
        });
        return;
      } catch (error) {
        if (!busy(error)) throw error;
        await sleep(25);
      }
    }
  }

  observe({status, headers, error}) {
    headers = new Headers(headers);
    const number = name => {
      const value = headers.get(name);
      return value !== null && /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null;
    };
    const remaining = number('x-ratelimit-remaining-tokens');
    const nativeCap = number('x-ratelimit-limit-tokens');
    const limited = status === 429 && error?.code === 'rate_limit_exceeded';
    const used = limited ? /\bUsed[:\s]+([\d,]+(?:\.\d+)?)/i.exec(error.message ?? '') : null;
    this.transaction(() => {
      const row = this.row();
      const now = this.now(row);
      const cap = nativeCap > 0 ? Math.min(row.cap, nativeCap) : row.cap;
      const reported = used ? Math.ceil(Number(used[1].replaceAll(',', ''))) : 0;
      const observed = remaining !== null ? Math.max(0, cap - remaining) : reported;
      // Header samples can arrive out of order. Keep the pessimistic high-water
      // mark for a full local window, plus ALL dispatched debt (never refunded).
      const remote = Math.min(cap, Math.max(row.remote_until > now ? row.remote_used : 0,
        Number.isSafeInteger(observed) ? observed : 0));
      this.db.prepare(`UPDATE buckets SET cap=?, blocked_until=?, remote_used=?, remote_until=?, last_now=? WHERE scope=?`)
        .run(cap, Math.max(row.blocked_until, limited ? now + retryDelayMs(headers, error.message, now) : 0),
          remote, observed > 0 ? Math.max(row.remote_until, now + row.window_ms) : row.remote_until, now, this.key);
    });
  }

  close() {this.db.close();}
}

async function rateError(response) {
  const reader = response.clone().body?.getReader();
  if (!reader) return null;
  let text = '';
  let bytes = 0;
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 65536) return null;
      text += decoder.decode(value, {stream: true});
    }
    return JSON.parse(text + decoder.decode())?.error ?? null;
  } catch {return null;}
  finally {void reader.cancel().catch(() => {});}
}

export function createOpenAITokenFetch({admission, fetch = globalThis.fetch,
  models, outputBound = 128000}) {
  if (!(admission instanceof SharedTokenAdmission) || !Array.isArray(models) || !models.length
    || models.some(model => typeof model !== 'string' || !model) || !positive(outputBound)) {
    throw new Error('Explicit OpenAI admission and exact model IDs required');
  }
  return async (input, init = {}) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (url.href !== 'https://api.openai.com/v1/responses' || (init.method ?? input.method ?? 'GET') !== 'POST') {
      return fetch(input, init);
    }
    if (typeof init.body !== 'string') throw new Error('OpenAI admission requires the native buffered JSON body');
    const payload = JSON.parse(init.body);
    if (!models.includes(payload.model)) return fetch(input, init);
    const opaqueInput = value => value && typeof value === 'object' && (
      ['input_image', 'input_audio', 'input_file'].includes(value.type)
      || Object.values(value).some(opaqueInput));
    if (opaqueInput(payload) || payload.previous_response_id || payload.conversation || payload.prompt
      || payload.tools?.some(tool => !['function', 'custom'].includes(tool.type))) {
      throw new Error('OpenAI admission has no token bound for remote context or hosted tools');
    }
    const output = payload.max_output_tokens ?? outputBound;
    if (!positive(output)) throw new Error('OpenAI request output bound unavailable');
    // Conservative UTF-8 byte bound, including tools/history, plus output cap.
    // This is local admission, not a claim of exact provider tokenizer parity.
    const tokens = Buffer.byteLength(init.body) + output;
    const signal = init.signal ?? input.signal;
    for (;;) {
      const grant = await admission.acquire(tokens, signal);
      let response;
      try {
        signal?.throwIfAborted();
        // No transport-error retry: uncertain sends and partial SSE stay native.
        response = await fetch(input, init);
      } finally {
        // Start expiry after headers/error, never before an arbitrarily stalled
        // worker actually sends. Cancellation still retains conservative debt.
        await admission.settle(grant);
      }
      const error = response.status === 429 ? await rateError(response) : null;
      for (;;) {
        signal?.throwIfAborted();
        try {
          admission.observe({status: response.status, headers: response.headers, error});
          break;
        } catch (observationError) {
          if (!busy(observationError)) throw observationError;
          // Keep the received response and debit while a peer owns the writer.
          await sleep(25, undefined, {signal});
        }
      }
      if (response.status !== 429 || error?.code !== 'rate_limit_exceeded'
        || response.headers.get('x-should-retry') === 'false') return response;
      await response.body?.cancel();
      // Only an explicit pre-stream rate rejection is retried, using the same
      // buffered bytes/headers/model, and reacquiring the shared org budget.
    }
  };
}

const ORIGINAL = Symbol.for('pi.openai-api-token-admission.original');

export function withOpenAIAPIAdmission(provider, {admission, models}) {
  if (provider?.id !== 'openai-api') throw new Error('Only the existing direct API provider may be wrapped');
  const base = provider[ORIGINAL] ?? provider;
  const result = {...base, [ORIGINAL]: base};
  for (const method of ['stream', 'streamSimple']) {
    if (typeof base[method] !== 'function') throw new Error('Native OpenAI API stream capability unavailable');
    result[method] = (model, context, options) => {
      if (model.provider !== 'openai-api' || model.api !== 'openai-responses'
        || model.baseUrl !== 'https://api.openai.com/v1' || !models.includes(model.id)) {
        return base[method](model, context, options);
      }
      return base[method](model, context, {...options,
        fetch: createOpenAITokenFetch({admission, models, outputBound: model.maxTokens,
          fetch: options?.fetch ?? globalThis.fetch})});
    };
  }
  return result;
}
