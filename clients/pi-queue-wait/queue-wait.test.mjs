// queue-wait.test.mjs — spec 227 test-author worker.
// Validator entry: node --test clients/pi-queue-wait/queue-wait.test.mjs
// Runs the pure unit suite (this file, zero external deps) plus the native
// replay suite (runNativeReplayTests from ./native-replay.mjs: real Pi
// adapter + fake loopback proxy + REAL extension loader + real ModelRuntime).
// Gate amendments baked in: api/compat import checks, exact body equality,
// method/protocol/redirect/final-URL negatives, allowedBaseUrls seam,
// random=0 ⇒ Retry-After + 1 ms, synthetic-exhaustion assertions with
// forbidden substrings, maxRetries passed explicitly (never pinned by the
// wrapper), non-POST/HTTPS/prefix/suffix/every-usage-field negatives, and
// clone-race cases.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createQueueWaitStream } from "./queue-wait.mjs";
import {
  MARKER_HEADER,
  MARKER_VALUE,
  QUEUE_TIMEOUT_HEADER,
  QUEUE_TIMEOUT_VALUE,
  PROXY_QUEUE_BODY,
  DEFAULT_BASE_URL,
  eligible503,
  runNativeReplayTests,
} from "./native-replay.mjs";

// ---------------------------------------------------------------------------
// AssistantMessageEventStream replica (push/end/async-iterate semantics from
// pi-ai utils/event-stream.js) so this suite runs without the pi-ai package.
// ---------------------------------------------------------------------------

function createEventStream() {
  const queue = [];
  const state = { ended: false, waiters: [], events: [] };
  const drain = () => {
    for (const w of state.waiters.splice(0)) w();
  };
  const stream = {
    push(event) {
      if (state.ended) return;
      state.events.push(event);
      queue.push(event);
      drain();
    },
    end() {
      if (state.ended) return;
      state.ended = true;
      drain();
    },
    async result() {
      while (!state.ended) await new Promise((resolve) => state.waiters.push(resolve));
      return state.events[state.events.length - 1];
    },
    [Symbol.asyncIterator]() {
      return {
        next: async () => {
          if (queue.length) return { value: queue.shift(), done: false };
          if (!state.ended) await new Promise((resolve) => state.waiters.push(resolve));
          if (queue.length) return { value: queue.shift(), done: false };
          return { value: undefined, done: true };
        },
      };
    },
  };
  return stream;
}

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

const USAGE_SCALAR_FIELDS = ["input", "output", "cacheRead", "cacheWrite", "totalTokens"];
const COST_SCALAR_FIELDS = ["input", "output", "cacheRead", "cacheWrite", "total"];
const ZERO_COST = () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 });
const ZERO_USAGE = () => ({
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  totalTokens: 0,
  cost: ZERO_COST(),
});

function makeModel(overrides = {}) {
  return {
    id: "glm-5.3-flash",
    provider: "zai",
    api: "openai-completions",
    baseUrl: DEFAULT_BASE_URL,
    contextWindow: 128000,
    maxTokens: 8192,
    ...overrides,
  };
}

function makeContext(overrides = {}) {
  return {
    system: "You are a queue-wait fixture.",
    messages: [
      {
        role: "user",
        content: [{ type: "text", text: "PROBE-SECRET-BODY ping" }],
        timestamp: 0,
      },
    ],
    tools: [],
    ...overrides,
  };
}

/**
 * Response-like object with fully controllable url/redirected/clone behavior —
 * `new Response()` cannot fake final URLs, so eligibility checks run against
 * this. cloneGate holds clone().text() until releaseCloneBody(); cloneReadFails
 * makes the CLONE's text() reject; cloneThrows makes clone() itself throw.
 */
function makeResponseLike(spec = {}) {
  const {
    status = 200,
    headers = {},
    body = "",
    url = "http://requested.invalid/api/coding/paas/v4/chat/completions",
    redirected = false,
  } = spec;
  const shared = { gates: [] };
  const build = (which) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Service Unavailable",
    headers: new Headers(headers),
    url,
    redirected,
    async text() {
      if (which === "clone") {
        if (spec.cloneReadFails) throw new Error("clone read failed");
        if (spec.cloneGate) await new Promise((resolve) => shared.gates.push(resolve));
      }
      return body;
    },
    clone() {
      if (spec.cloneThrows) throw new Error("clone() unsupported");
      return build("clone");
    },
  });
  const response = build("original");
  response.releaseCloneBody = () => {
    for (const resolve of shared.gates.splice(0)) resolve();
  };
  return response;
}

/**
 * Low-level fetch the WRAPPER wraps and observes: scripted successive raw
 * responses. Step keys: {status, headers, body, url, redirected, cloneGate,
 * cloneReadFails, cloneThrows, networkFail}.
 */
function fakeFetch(script) {
  const calls = [];
  const responses = [];
  const fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const spec = script.length > 1 ? script.shift() : (script[0] ?? {});
    calls.push({
      url,
      method: init.method ?? "GET",
      headers: init.headers,
      body: init.body,
      signal: init.signal,
    });
    if (spec.networkFail) throw new TypeError("fetch failed");
    const response = makeResponseLike({ url, ...spec });
    responses.push(response);
    return response;
  };
  return { fetch, calls, responses };
}

function nativeErrorEvent(spec = {}, options = {}) {
  const aborted = options.signal?.aborted === true;
  const error = {
    role: spec.role ?? "assistant",
    content: spec.content ?? [],
    api: "openai-completions",
    provider: "zai",
    model: "glm-5.3-flash",
    usage: spec.usage ?? ZERO_USAGE(),
    stopReason: aborted ? "aborted" : "error",
    errorMessage: spec.errorMessage ?? `503 ${PROXY_QUEUE_BODY}`,
    timestamp: 0,
  };
  if (spec.omitRole) delete error.role;
  if (spec.omitUsage) delete error.usage;
  return {
    type: "error",
    reason: aborted ? "aborted" : "error",
    error,
  };
}

function successEvents() {
  const partial = () => ({
    role: "assistant",
    content: [],
    api: "openai-completions",
    provider: "zai",
    model: "glm-5.3-flash",
    usage: ZERO_USAGE(),
    stopReason: "pending",
    timestamp: 0,
  });
  return [
    { type: "start", partial: partial() },
    { type: "text_start", partial: partial() },
    { type: "text_delta", delta: "Hi", partial: partial() },
    {
      type: "done",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "Hi" }],
        api: "openai-completions",
        provider: "zai",
        model: "glm-5.3-flash",
        usage: { ...ZERO_USAGE(), input: 1, output: 1, totalTokens: 2 },
        stopReason: "stop",
        timestamp: 0,
      },
    },
  ];
}

/**
 * streamSimple replica with the native adapter's observable contract: calls
 * options.fetch once per attempt, treats a non-2xx response as ONE terminal
 * error event (empty content, zero usage, "<status> <body>" message), and a
 * 2xx as the full success sequence. Behavior steps (optional): {emit,
 * thenError, networkFail, requestMethod, pathOverride, requestBodyOverride}.
 * An empty script means "plain attempt, let options.fetch decide".
 */
function fakeStreamSimple(behavior, attempts = []) {
  return function streamSimple(model, context, options = {}) {
    const stream = createEventStream();
    attempts.push({ model, context, options });
    (async () => {
      try {
        const step = behavior.length > 1 ? behavior.shift() : (behavior[0] ?? {});
        if (step.emit) {
          for (const event of step.emit) stream.push(event);
          if (step.thenError) stream.push(nativeErrorEvent(step.thenError, options));
          return;
        }
        if (step.networkFail) throw new TypeError("fetch failed");
        const url = `${model.baseUrl}${step.pathOverride ?? "/chat/completions"}`;
        const body =
          step.requestBodyOverride ??
          JSON.stringify({ model: model.id, messages: context.messages, system: context.system });
        const response = await options.fetch(url, {
          method: step.requestMethod ?? "POST",
          headers: { "content-type": "application/json", authorization: "Bearer sk-test-fake-key" },
          body,
          signal: options.signal,
        });
        if (!response.ok) {
          const text = await response.text();
          stream.push(
            nativeErrorEvent(
              { errorMessage: `${response.status} ${text}`, ...(step.thenError ?? {}) },
              options,
            ),
          );
          return;
        }
        for (const event of successEvents()) stream.push(event);
      } catch (error) {
        stream.push(nativeErrorEvent({ errorMessage: String(error?.message ?? error) }, options));
      } finally {
        stream.end();
      }
    })();
    return stream;
  };
}

/** Deterministic deps: virtual clock, recorded immediate sleeps, seeded jitter. */
function baseDeps(overrides = {}) {
  const waits = [];
  const sleeps = [];
  let virtualNow = Date.parse("2026-09-14T12:00:00Z");
  const deps = {
    createEventStream,
    now: () => virtualNow,
    sleep: (ms, signal) => {
      sleeps.push({ ms, signal });
      if (signal?.aborted) return Promise.reject(new Error("aborted"));
      return Promise.resolve();
    },
    random: () => 0,
    onWait: (info) => waits.push(info),
    maxWaitMs: 1_800_000,
    maxRejections: 32,
    ...overrides,
  };
  return { deps, waits, sleeps, setNow: (ms) => (virtualNow = ms) };
}

function errorEventsOf(events) {
  return events.filter((event) => event.type === "error");
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function bounded(promise, label, timeoutMs = 2_000) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs} ms`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function waitUntil(predicate, label, timeoutMs = 500) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error(`${label} timed out after ${timeoutMs} ms`);
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

async function drain(stream) {
  return bounded(
    (async () => {
      const events = [];
      for await (const event of stream) events.push(event);
      return events;
    })(),
    "event stream drain",
  );
}

function assertSingleNativeError(events, reason, message) {
  assert.equal(events.length, 1, "exactly one terminal event must be emitted");
  assert.equal(events[0].type, "error");
  assert.equal(events[0].reason, reason);
  assert.equal(events[0].error?.role, "assistant");
  assert.equal(events[0].error?.stopReason, reason);
  assert.equal(events[0].error?.errorMessage, message);
  assert.deepEqual(events[0].error?.content, []);
  assert.deepEqual(events[0].error?.usage, ZERO_USAGE());
  assert.equal("cost" in (events[0].error ?? {}), false);
}

// ---------------------------------------------------------------------------
// Native replay suite (real adapter / real loader / real ModelRuntime).
// ---------------------------------------------------------------------------

await runNativeReplayTests();

for (const [reason, stopReason] of [["stop", "error"], ["error", "stop"], [undefined, "error"], ["error", undefined]]) {
  test(`unit: fail closed on terminal semantics ${reason}/${stopReason}`, async () => {
    const { fetch, calls } = fakeFetch([eligible503(), { status: 200 }]);
    const native = fakeStreamSimple([]);
    const { deps } = baseDeps({
      streamSimple(model, context, options) {
        const outer = createEventStream();
        (async () => {
          try {
            for await (const event of native(model, context, options)) {
              if (event.type === "error") {
                event.reason = reason;
                event.error.stopReason = stopReason;
              }
              outer.push(event);
            }
          } finally { outer.end(); }
        })();
        return outer;
      },
    });
    const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
    assert.equal(calls.length, 1, "unknown terminal semantics must not replay a request");
    assert.equal(events[0].reason, reason);
    assert.equal(events[0].error.stopReason, stopReason);
  });
}

test("unit: throwing injected clock cannot erase an original failure", async () => {
  const { deps } = baseDeps({
    streamSimple: () => { throw new Error("original failure"); },
    now: () => { throw new Error("clock unavailable"); },
  });
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), {}));
  assert.equal(events.length, 1);
  assert.equal(events[0].reason, "error");
  assert.equal(events[0].error.errorMessage, "original failure");
  assert.ok(Number.isFinite(events[0].error.timestamp));
});

test("unit: a message-only aborted error is not cancellation", async () => {
  const { fetch, calls } = fakeFetch([eligible503(), { status: 200 }]);
  const { deps } = baseDeps({
    streamSimple: fakeStreamSimple([]),
    sleep: async () => { throw new Error("aborted"); },
  });
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(calls.length, 1);
  assert.equal(events[0].reason, "error");
  assert.equal(events[0].error.stopReason, "error");
  assert.equal(events[0].error.errorMessage, "aborted");
});

// ---------------------------------------------------------------------------
// 1. Eligibility gate — a single upstream call, native error surfaced once.
// ---------------------------------------------------------------------------

const RESPOND_MUTATIONS = {
  noMarkerHeader: { status: 503, headers: { [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY },
  noQueueTimeoutStamp: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY },
  stampsOn401: { status: 401, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY },
  stampsOn403: { status: 403, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY },
  stampsOn429: { status: 429, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY },
  stampsOn500: { status: 500, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY },
  bodyWrongText: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: "service unavailable\n" },
  bodyPrefixOnly: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY.slice(0, 40) },
  bodySuffixOnly: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY.slice(20) },
  bodyMissingTrailingNewline: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY.trimEnd() },
  bodyExtraNewline: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: `${PROXY_QUEUE_BODY}\n` },
  bodyDashSwapped: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY.replace("—", "--") },
  foreignRedirect: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY, redirected: true, url: "http://169.254.1.1/elsewhere" },
  finalUrlDiffers: { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" }, body: PROXY_QUEUE_BODY, redirected: false, url: "http://169.254.1.1/elsewhere" },
};

for (const [name, respond] of Object.entries(RESPOND_MUTATIONS)) {
  test(`unit: ineligible passthrough — ${name}`, async () => {
    const attempts = [];
    const { deps, waits, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
    const { fetch, calls } = fakeFetch([respond]);
    const events = await drain(
      createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, apiKey: "sk-test-fake-key", maxRetries: 0 }),
    );
    assert.equal(attempts.length, 1, "exactly one upstream attempt");
    assert.equal(calls.length, 1, "exactly one observed fetch");
    const errors = errorEventsOf(events);
    assert.equal(errors.length, 1, "native error surfaced exactly once");
    assert.deepEqual(
      errors[0],
      nativeErrorEvent({ errorMessage: `${respond.status} ${respond.body}` }),
      "native status, text, and terminal event must pass through untouched",
    );
    assert.equal(sleeps.length, 0, "no admission wait for ineligible responses");
    assert.ok(waits.every((info) => info == null), "waiting UI never shows for ineligible responses");
  });
}

const MODEL_MUTATIONS = {
  wrongProvider: { provider: "other" },
  wrongModelId: { id: "glm-4.7-air" },
  otherHost: { baseUrl: "http://10.0.0.1:8766/api/coding/paas/v4" },
  otherPort: { baseUrl: "http://127.0.0.1:9999/api/coding/paas/v4" },
  otherPath: { baseUrl: "http://127.0.0.1:8766/other/v4" },
  httpsScheme: { baseUrl: "https://127.0.0.1:8766/api/coding/paas/v4" },
};

for (const [name, overrides] of Object.entries(MODEL_MUTATIONS)) {
  test(`unit: ineligible passthrough — ${name}`, async () => {
    const attempts = [];
    const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
    const { fetch, calls } = fakeFetch([eligible503()]);
    const events = await drain(
      createQueueWaitStream(deps)(makeModel(overrides), makeContext(), { fetch, apiKey: "sk-test-fake-key", maxRetries: 0 }),
    );
    assert.equal(attempts.length, 1, "exactly one upstream attempt");
    assert.equal(calls.length, 1, "exactly one observed fetch");
    const errors = errorEventsOf(events);
    assert.equal(errors.length, 1);
    assert.equal(sleeps.length, 0, "no admission wait off the pinned lane");
  });
}

test("unit: ineligible passthrough — non-POST request method", async () => {
  const attempts = [];
  const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([{ requestMethod: "GET" }], attempts) });
  const { fetch, calls } = fakeFetch([
    { status: 503, headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE }, body: PROXY_QUEUE_BODY },
  ]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, apiKey: "k", maxRetries: 0 }));
  assert.equal(attempts.length, 1);
  assert.equal(calls[0].method, "GET");
  assert.equal(errorEventsOf(events).length, 1);
  assert.equal(sleeps.length, 0);
});

for (const [name, modelOverrides, pathOverride] of [
  ["request URL has a query", {}, "/chat/completions?retry=1"],
  ["request URL has credentials", { baseUrl: "http://fixture:secret@127.0.0.1:8766/api/coding/paas/v4" }, undefined],
  ["request URL has a fragment", {}, "/chat/completions#fragment"],
]) {
  test(`unit: strict full URL — ${name}`, async () => {
    const attempts = [];
    const { deps, sleeps } = baseDeps({
      streamSimple: fakeStreamSimple([{ pathOverride }], attempts),
      maxRejections: 1,
    });
    const { fetch, calls } = fakeFetch([eligible503()]);
    const events = await drain(
      createQueueWaitStream(deps)(makeModel(modelOverrides), makeContext(), { fetch, maxRetries: 0 }),
    );
    assert.equal(attempts.length, 1, "only the exact credential-free, query-free, fragment-free endpoint qualifies");
    assert.equal(calls.length, 1);
    assert.equal(sleeps.length, 0);
    assertSingleNativeError(events, "error", `503 ${PROXY_QUEUE_BODY}`);
  });
}

test("unit: strict full URL — final URL differing only in query is ineligible", async () => {
  const attempts = [];
  const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts), maxRejections: 1 });
  const requestedUrl = `${DEFAULT_BASE_URL}/chat/completions`;
  const { fetch, calls } = fakeFetch([
    { ...eligible503(), url: `${requestedUrl}?redirected=1` },
    { status: 200, headers: {}, body: "" },
  ]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 1, "a final URL query mismatch must never authorize redispatch");
  assert.equal(calls.length, 1);
  assert.equal(sleeps.length, 0);
  assertSingleNativeError(events, "error", `503 ${PROXY_QUEUE_BODY}`);
});

// ---------------------------------------------------------------------------
// 2. Happy retry path.
// ---------------------------------------------------------------------------

test("unit: eligible rejection is retried transparently to a full success stream", async () => {
  const attempts = [];
  const { deps, waits, sleeps } = baseDeps({
    streamSimple: fakeStreamSimple([], attempts),
    random: () => 0.5,
  });
  const callerSignal = new AbortController().signal;
  const { fetch, calls } = fakeFetch([eligible503({ retryAfter: 7 }), { status: 200, headers: {}, body: "" }]);
  const events = await drain(
    createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, signal: callerSignal, apiKey: "sk-test-fake-key", maxRetries: 0 }),
  );
  assert.equal(attempts.length, 2, "exactly two upstream attempts");
  assert.equal(errorEventsOf(events).length, 0, "client must see no error event");
  assert.equal(events[events.length - 1]?.type, "done");
  assert.equal(sleeps.length, 1);
  assert.ok(sleeps[0].ms > 7000, "jitter must be strictly positive");
  assert.ok(sleeps[0].ms < 7000 + 60_000, "jitter must stay bounded");
  assert.ok(waits.some((info) => info != null), "onWait fired during the admission wait");
  assert.equal(waits[waits.length - 1], null, "waiting UI cleared after success");
  assert.equal(calls[0].body, calls[1].body, "identical serialized body on both attempts");
  assert.ok(String(calls[0].body).includes("PROBE-SECRET-BODY"), "body is the caller's, not a reserialization");
  assert.equal(attempts[0].options.signal, callerSignal, "caller signal object passed through unchanged");
  assert.equal(attempts[1].options.signal, callerSignal);
});

test("unit: random=0 yields exactly Retry-After + 1 ms", async () => {
  const attempts = [];
  const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts), random: () => 0 });
  const { fetch } = fakeFetch([eligible503({ retryAfter: 7 }), { status: 200, headers: {}, body: "" }]);
  await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.deepEqual(sleeps.map((entry) => entry.ms), [7001]);
});

// ---------------------------------------------------------------------------
// 3. Retry-After handling.
// ---------------------------------------------------------------------------

test("unit: Retry-After as HTTP-date resolves against the injected clock", async () => {
  const attempts = [];
  const { deps, sleeps, setNow } = baseDeps({ streamSimple: fakeStreamSimple([], attempts), random: () => 0 });
  setNow(Date.parse("2026-09-14T12:00:00Z"));
  const { fetch } = fakeFetch([
    {
      status: 503,
      headers: {
        [MARKER_HEADER]: MARKER_VALUE,
        [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE,
        "retry-after": "Mon, 14 Sep 2026 12:00:09 GMT",
      },
      body: PROXY_QUEUE_BODY,
    },
    { status: 200, headers: {}, body: "" },
  ]);
  await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.deepEqual(sleeps.map((entry) => entry.ms), [9001]);
});

const RETRY_AFTER_FALLBACKS = {
  garbageRetryAfter: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "soon-ish" },
  missingRetryAfter: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE },
  zeroRetryAfter: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "0" },
};

for (const [name, headers] of Object.entries(RETRY_AFTER_FALLBACKS)) {
  test(`unit: missing/invalid Retry-After falls back conservatively — ${name}`, async () => {
    const attempts = [];
    const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts), random: () => 0 });
    const { fetch } = fakeFetch([{ status: 503, headers, body: PROXY_QUEUE_BODY }, { status: 200, headers: {}, body: "" }]);
    await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
    assert.equal(attempts.length, 2, "still retried, exactly once");
    assert.equal(sleeps.length, 1);
    assert.ok(sleeps[0].ms >= 5000, `fallback must stay non-hot (>=5s, proxy floor), saw ${sleeps[0].ms}`);
    assert.ok(Number.isFinite(sleeps[0].ms));
  });
}

// ---------------------------------------------------------------------------
// 4. No retry after ANY forwarded output or non-empty native error payload.
// ---------------------------------------------------------------------------

test("unit: no retry once a stream event was forwarded to the client", async () => {
  const attempts = [];
  const { deps, sleeps } = baseDeps({
    streamSimple: fakeStreamSimple([
      { emit: [{ type: "start", partial: { role: "assistant", content: [], usage: ZERO_USAGE() } }], thenError: {} },
    ], attempts),
  });
  const { fetch } = fakeFetch([]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 1, "output started → never retried");
  const errors = errorEventsOf(events);
  assert.equal(errors.length, 1);
  assert.equal(sleeps.length, 0);
});

test("unit: no retry when the native error carries content", async () => {
  const attempts = [];
  const { deps } = baseDeps({
    streamSimple: fakeStreamSimple([
      { thenError: { content: [{ type: "text", text: "partial answer before dying" }] } },
    ], attempts),
  });
  const { fetch, calls } = fakeFetch([eligible503()]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 1);
  assert.equal(calls.length, 1, "content guard is exercised after an eligible captured response");
  const errors = errorEventsOf(events);
  assert.equal(errors.length, 1);
  assert.deepEqual(errors[0].error?.content, [{ type: "text", text: "partial answer before dying" }]);
});

for (const field of USAGE_SCALAR_FIELDS) {
  test(`unit: no retry when usage.${field} is non-zero`, async () => {
    const attempts = [];
    const { deps, sleeps } = baseDeps({
      streamSimple: fakeStreamSimple([{ thenError: { usage: { ...ZERO_USAGE(), [field]: 3 } } }], attempts),
    });
    const { fetch, calls } = fakeFetch([eligible503()]);
    await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
    assert.equal(attempts.length, 1, `non-zero usage.${field} must disqualify the retry`);
    assert.equal(calls.length, 1, "the fully eligible response gate must be exercised");
    assert.equal(sleeps.length, 0);
  });
}

for (const field of COST_SCALAR_FIELDS) {
  test(`unit: no retry when usage.cost.${field} is non-zero`, async () => {
    const attempts = [];
    const { deps, sleeps } = baseDeps({
      streamSimple: fakeStreamSimple(
        [{ thenError: { usage: { ...ZERO_USAGE(), cost: { ...ZERO_COST(), [field]: 0.25 } } } }],
        attempts,
      ),
    });
    const { fetch, calls } = fakeFetch([eligible503()]);
    await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
    assert.equal(attempts.length, 1, `non-zero usage.cost.${field} must disqualify the retry`);
    assert.equal(calls.length, 1, "the fully eligible response gate must be exercised");
    assert.equal(sleeps.length, 0);
  });
}

for (const field of USAGE_SCALAR_FIELDS) {
  test(`unit: missing usage.${field} fails closed`, async () => {
    const usage = ZERO_USAGE();
    delete usage[field];
    const attempts = [];
    const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([{ thenError: { usage } }], attempts) });
    const { fetch, calls } = fakeFetch([eligible503()]);
    await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
    assert.equal(attempts.length, 1);
    assert.equal(calls.length, 1);
    assert.equal(sleeps.length, 0, `missing usage.${field} must not authorize a retry`);
  });
}

for (const field of COST_SCALAR_FIELDS) {
  test(`unit: missing usage.cost.${field} fails closed`, async () => {
    const usage = ZERO_USAGE();
    delete usage.cost[field];
    const attempts = [];
    const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([{ thenError: { usage } }], attempts) });
    const { fetch, calls } = fakeFetch([eligible503()]);
    await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
    assert.equal(attempts.length, 1);
    assert.equal(calls.length, 1);
    assert.equal(sleeps.length, 0, `missing usage.cost.${field} must not authorize a retry`);
  });
}

for (const [name, errorShape] of [
  ["role", { omitRole: true }],
  ["usage", { omitUsage: true }],
]) {
  test(`unit: missing assistant error ${name} fails closed`, async () => {
    const attempts = [];
    const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([{ thenError: errorShape }], attempts) });
    const { fetch, calls } = fakeFetch([eligible503()]);
    await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
    assert.equal(attempts.length, 1);
    assert.equal(calls.length, 1);
    assert.equal(sleeps.length, 0, `missing ${name} must not authorize a retry`);
  });
}

// ---------------------------------------------------------------------------
// 5. Budgets, exhaustion, abort.
// ---------------------------------------------------------------------------

test("unit: maxRejections exhaustion surfaces a synthetic, retry-safe error", async () => {
  const attempts = [];
  const { deps, waits } = baseDeps({ streamSimple: fakeStreamSimple([], attempts), maxRejections: 2 });
  const { fetch } = fakeFetch([eligible503({ retryAfter: 1 })]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 3, "N rejections allowed + the final one = N+1 attempts");
  const errors = errorEventsOf(events);
  assert.equal(errors.length, 1, "single terminal error, nothing forwarded before it");
  const message = String(errors[0].error?.errorMessage ?? "");
  assert.match(message, /retr|exhaust|attempts|gave up/i, "error must explain the exhaustion");
  for (const forbidden of ["503", "rate limit", "proxy queue wait exceeded"]) {
    assert.ok(!message.toLowerCase().includes(forbidden.toLowerCase()), `exhaustion text must not contain "${forbidden}"`);
  }
  assert.equal(waits[waits.length - 1], null, "waiting UI cleared on exhaustion");
});

test("unit: maxWaitMs stops admission waits and never re-surfaces the 503", async () => {
  const attempts = [];
  const { deps, sleeps, waits } = baseDeps({
    streamSimple: fakeStreamSimple([], attempts),
    maxWaitMs: 12_000,
  });
  const { fetch } = fakeFetch([eligible503({ retryAfter: 10 })]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.ok(attempts.length <= 2, "attempts stop once the budget would be exceeded");
  const total = sleeps.reduce((sum, entry) => sum + entry.ms, 0);
  assert.ok(total <= 12_000, `cumulative admission wait must respect the budget, saw ${total}`);
  const errors = errorEventsOf(events);
  assert.equal(errors.length, 1);
  const message = String(errors[0].error?.errorMessage ?? "");
  assert.ok(!message.includes("503"), "budget exhaustion must not echo the raw status");
  assert.ok(!message.includes(PROXY_QUEUE_BODY.trim()), "budget exhaustion must not re-surface the last rejection");
  assert.equal(waits[waits.length - 1], null);
});

test("unit: budget caps admission waits only — a started stream runs to completion", async () => {
  const attempts = [];
  const { deps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts), maxWaitMs: 12_000 });
  const { fetch } = fakeFetch([eligible503({ retryAfter: 10 }), { status: 200, headers: {}, body: "" }]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 2);
  assert.equal(errorEventsOf(events).length, 0);
  assert.equal(events[events.length - 1]?.type, "done", "successful stream must not be cut by the admission budget");
});

test("unit: abort while sleeping — no further upstream call, waiting UI cleared", async () => {
  const attempts = [];
  let releaseAborted;
  const { deps, waits } = baseDeps({
    streamSimple: fakeStreamSimple([], attempts),
    sleep: () =>
      new Promise((_, reject) => {
        releaseAborted = () => reject(new Error("aborted"));
      }),
  });
  const controller = new AbortController();
  const { fetch } = fakeFetch([eligible503({ retryAfter: 30 })]);
  const streamPromise = drain(
    createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, signal: controller.signal, maxRetries: 0 }),
  );
  try {
    await waitUntil(() => typeof releaseAborted === "function", "admission sleep start");
    assert.equal(attempts.length, 1, "first attempt reached the fake proxy");
    controller.abort();
    releaseAborted();
    const events = await streamPromise;
    assert.equal(attempts.length, 1);
    const errors = errorEventsOf(events);
    assert.equal(errors.length, 1);
    assert.equal(errors[0].reason === "aborted" || errors[0].error?.stopReason === "aborted", true);
    assert.equal(waits[waits.length - 1], null, "waiting UI cleared on abort");
  } finally {
    controller.abort();
    releaseAborted?.();
    await streamPromise.catch(() => {});
  }
});

test("unit: abort before the first fetch — zero upstream calls", async () => {
  const attempts = [];
  const { deps, waits } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
  const controller = new AbortController();
  controller.abort();
  const { fetch, calls } = fakeFetch([eligible503()]);
  const events = await drain(
    createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, signal: controller.signal, maxRetries: 0 }),
  );
  assert.equal(attempts.length + calls.length, 0, "no upstream call may be issued after abort");
  const errors = errorEventsOf(events);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].reason === "aborted" || errors[0].error?.stopReason === "aborted", true);
  assert.equal(waits[waits.length - 1], null);
});

test("unit: non-abort sleep rejection preserves the failure as one native error", async () => {
  const attempts = [];
  const waits = [];
  const { deps } = baseDeps({
    streamSimple: fakeStreamSimple([], attempts),
    sleep: () => Promise.reject(new Error("synthetic sleep failure")),
    onWait: (info) => waits.push(info),
  });
  const { fetch, calls } = fakeFetch([eligible503()]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 1);
  assert.equal(calls.length, 1);
  assertSingleNativeError(events, "error", "synthetic sleep failure");
  assert.equal(waits[waits.length - 1], null, "waiting UI must clear after a sleep failure");
});

for (const [name, streamSimple, message] of [
  [
    "synchronous streamSimple throw",
    () => {
      throw new Error("synchronous stream failure");
    },
    "synchronous stream failure",
  ],
  [
    "async iterator throw",
    () => ({
      [Symbol.asyncIterator]() {
        return {
          async next() {
            throw new Error("iterator failure");
          },
        };
      },
    }),
    "iterator failure",
  ],
]) {
  test(`unit: ${name} emits exactly one native terminal error`, async () => {
    const waits = [];
    const { deps } = baseDeps({ streamSimple, onWait: (info) => waits.push(info) });
    const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { maxRetries: 0 }));
    assertSingleNativeError(events, "error", message);
    assert.equal(waits[waits.length - 1], null, "waiting UI must clear after an engine throw");
  });
}

// ---------------------------------------------------------------------------
// 6. Clone races.
// ---------------------------------------------------------------------------

test("unit: clone body completing after the native error still yields the retry", async () => {
  const attempts = [];
  const { deps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
  const { fetch, responses } = fakeFetch([
    {
      status: 503,
      headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" },
      body: PROXY_QUEUE_BODY,
      cloneGate: true,
    },
    { status: 200, headers: {}, body: "" },
  ]);
  const streamPromise = drain(
    createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }),
  );
  try {
    await waitUntil(() => responses.length === 1, "clone capture start");
    assert.equal(attempts.length, 1, "retry decision is parked until the clone body resolves");
    for (const response of responses) response.releaseCloneBody?.();
    const events = await streamPromise;
    assert.equal(attempts.length, 2, "late clone body must complete the eligible verdict and retry");
    assert.equal(errorEventsOf(events).length, 0);
    assert.equal(events[events.length - 1]?.type, "done");
  } finally {
    for (const response of responses) response.releaseCloneBody?.();
    await streamPromise.catch(() => {});
  }
});

test("unit: clone read failure fails closed — the native error surfaces, no retry", async () => {
  const attempts = [];
  const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
  const { fetch } = fakeFetch([
    {
      status: 503,
      headers: { [MARKER_HEADER]: MARKER_VALUE, [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE, "retry-after": "1" },
      body: PROXY_QUEUE_BODY,
      cloneReadFails: true,
    },
  ]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 1, "unreadable body must NOT authorize a retry");
  assert.equal(errorEventsOf(events).length, 1);
  assert.equal(sleeps.length, 0);
});

test("unit: a later underlying fetch failure clears an earlier eligible capture", async () => {
  const attempts = [];
  const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
  const { fetch } = fakeFetch([
    eligible503({ retryAfter: 1 }),
    { networkFail: true },
  ]);
  const events = await drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  assert.equal(attempts.length, 2, "one retry after the eligible rejection");
  assert.equal(sleeps.length, 1, "the eligible wait happened");
  const errors = errorEventsOf(events);
  assert.equal(errors.length, 1, "the fresh failure terminates — no stale-capture third attempt");
});

test("unit: stale clone from an earlier internal fetch cannot authorize outer redispatch", async () => {
  const attempts = [];
  const terminalGate = deferred();
  const streamSimple = (model, context, options = {}) => {
    const stream = createEventStream();
    attempts.push({ model, context, options });
    (async () => {
      let failure;
      try {
        const url = `${model.baseUrl}/chat/completions`;
        const init = { method: "POST", body: "PROBE-SECRET-BODY", signal: options.signal };
        await options.fetch(url, init);
        await options.fetch(url, init);
      } catch (error) {
        failure = error;
      }
      await terminalGate.promise;
      stream.push(nativeErrorEvent({ errorMessage: String(failure?.message ?? failure) }, options));
      stream.end();
    })().catch((error) => {
      stream.push(nativeErrorEvent({ errorMessage: String(error?.message ?? error) }, options));
      stream.end();
    });
    return stream;
  };
  const { deps, sleeps } = baseDeps({ streamSimple });
  const { fetch, calls, responses } = fakeFetch([
    { ...eligible503(), cloneGate: true },
    { networkFail: true },
  ]);
  const streamPromise = drain(createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, maxRetries: 0 }));
  try {
    await waitUntil(() => calls.length === 2 && responses.length === 1, "two internal fetches");
    responses[0].releaseCloneBody();
    await new Promise((resolve) => setTimeout(resolve, 0));
    terminalGate.resolve();
    const events = await streamPromise;
    assert.equal(attempts.length, 1, "stale capture from the first internal fetch must not trigger a second outer attempt");
    assert.equal(calls.length, 2, "only the native adapter's two internal fetches are expected");
    assert.equal(sleeps.length, 0, "stale evidence must not authorize an admission wait");
    assertSingleNativeError(events, "error", "fetch failed");
  } finally {
    for (const response of responses) response.releaseCloneBody?.();
    terminalGate.resolve();
    await streamPromise.catch(() => {});
  }
});

test("unit: abort during a stalled clone terminates promptly without retry", async () => {
  const attempts = [];
  const controller = new AbortController();
  const { deps, waits, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
  const { fetch, calls, responses } = fakeFetch([{ ...eligible503({ retryAfter: 30 }), cloneGate: true }]);
  const streamPromise = drain(
    createQueueWaitStream(deps)(makeModel(), makeContext(), {
      fetch,
      signal: controller.signal,
      maxRetries: 0,
    }),
  );
  let outcome;
  try {
    await waitUntil(() => calls.length === 1 && responses.length === 1, "stalled clone capture");
    controller.abort();
    outcome = await Promise.race([
      streamPromise.then((events) => ({ events })),
      new Promise((resolve) => setTimeout(() => resolve({ timedOut: true }), 100)),
    ]);
  } finally {
    for (const response of responses) response.releaseCloneBody?.();
  }
  const events = outcome?.events ?? (await streamPromise);
  assert.notEqual(outcome?.timedOut, true, "abort must not wait for a stalled clone body");
  assert.equal(attempts.length, 1);
  assert.equal(calls.length, 1);
  assert.equal(sleeps.length, 0);
  assert.equal(errorEventsOf(events).length, 1, "abort emits exactly one terminal error");
  assert.equal(events.length, 1);
  assert.equal(events[0].reason, "aborted");
  assert.equal(events[0].error?.stopReason, "aborted");
  assert.equal(waits[waits.length - 1], null, "waiting UI clears on capture abort");
});

// ---------------------------------------------------------------------------
// 7. Hygiene and concurrency.
// ---------------------------------------------------------------------------

test("unit: no bearer tokens or request bodies leak through surfaced errors or wait info", async () => {
  const attempts = [];
  const { deps, waits } = baseDeps({ streamSimple: fakeStreamSimple([], attempts), maxRejections: 1 });
  const { fetch } = fakeFetch([eligible503({ retryAfter: 1 })]);
  const events = await drain(
    createQueueWaitStream(deps)(makeModel(), makeContext(), { fetch, apiKey: "sk-test-fake-key", maxRetries: 0 }),
  );
  const rendered = JSON.stringify([...events, ...waits]);
  for (const secret of ["sk-test-fake-key", "Bearer ", "PROBE-SECRET-BODY"]) {
    assert.ok(!rendered.includes(secret), `secret "${secret}" must never surface in events or wait info`);
  }
});

test("unit: concurrent requests isolate scripts and wait state", async () => {
  const attempts = [];
  const scripts = new Map();
  scripts.set("SLOW", [eligible503({ retryAfter: 5 }), { status: 200, headers: {}, body: "" }]);
  scripts.set("FAST", [{ status: 200, headers: {}, body: "" }]);
  // Route responses per lane via fetch: each lane gets its own response script.
  const laneFetch = (tag) => fakeFetch(scripts.get(tag)).fetch;
  const { deps, sleeps } = baseDeps({ streamSimple: fakeStreamSimple([], attempts) });
  const streamFn = createQueueWaitStream(deps);
  const slowPromise = drain(
    streamFn(makeModel(), makeContext({ system: "SLOW lane" }), { fetch: laneFetch("SLOW"), maxRetries: 0 }),
  );
  const fastPromise = drain(
    streamFn(makeModel(), makeContext({ system: "FAST lane" }), { fetch: laneFetch("FAST"), maxRetries: 0 }),
  );
  const [slow, fast] = await Promise.all([slowPromise, fastPromise]);
  const slowAttempts = attempts.filter((entry) => entry.context.system.includes("SLOW"));
  const fastAttempts = attempts.filter((entry) => entry.context.system.includes("FAST"));
  assert.equal(slowAttempts.length, 2, "slow lane retried once");
  assert.equal(fastAttempts.length, 1, "fast lane never waited");
  assert.equal(errorEventsOf(slow).length + errorEventsOf(fast).length, 0);
  assert.equal(slow[slow.length - 1]?.type, "done");
  assert.equal(fast[fast.length - 1]?.type, "done");
  assert.ok(sleeps.length >= 1, "the slow lane's admission wait was recorded");
});
