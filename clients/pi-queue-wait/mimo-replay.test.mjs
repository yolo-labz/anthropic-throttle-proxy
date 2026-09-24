// Actual Pi 0.85.1 adapter, synthetic auth, loopback HTTP only. Never calls :8773.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createQueueWaitStream } from "./queue-wait.mjs";
import {
  PI_CODING_AGENT_ROOT, loadPiAi, loadModelRuntimeModule,
  loadQueueWaitExtensionRegistration, createFakeProxy, eligible503, sse200,
  PROXY_QUEUE_BODY, MARKER_HEADER, QUEUE_TIMEOUT_HEADER,
} from "./native-replay.mjs";

const BASE = "http://127.0.0.1:8773/v1";
const model = (patch = {}) => ({
  id: "mimo-v2.6-pro", name: "MiMo fixture", provider: "mimo-desktop",
  api: "openai-completions", baseUrl: BASE, reasoning: false, input: ["text"],
  cost: { input: 1, output: 2, cacheRead: 0.1, cacheWrite: 0.2 },
  contextWindow: 1000000, maxTokens: 8192, ...patch,
});
const context = () => ({
  systemPrompt: "Hermetic replay.",
  messages: [{ role: "user", content: [{ type: "text", text: "ping" }], timestamp: 0 }],
  tools: [{ name: "probe", description: "fixture", parameters: { type: "object", properties: {} } }],
});
async function drain(stream) {
  const events = [];
  for await (const event of stream) events.push(event);
  return events;
}

// Preserve the logical request/final URL presented to the wrapper, but send
// ALL bytes to an ephemeral fake server. No production DI origin allowlist.
async function fixture(script) {
  const pi = await loadPiAi();
  const proxy = createFakeProxy(script);
  const origin = new URL(await proxy.baseUrl()).origin;
  const originals = [], calls = [], waits = [], delays = [];
  const fetch = async (input, init) => {
    const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url);
    calls.push({ url: url.href, init });
    const res = await globalThis.fetch(origin + url.pathname + url.search, init);
    Object.defineProperty(res, "url", { value: url.href, configurable: true });
    originals.push(res);
    return res;
  };
  return {
    pi, proxy, originals, calls, waits, delays, fetch,
    stream(deps = {}) {
      return createQueueWaitStream({
        streamSimple: pi.streamSimple, createEventStream: pi.createEventStream,
        random: () => 0, sleep: async (ms) => { delays.push(ms); },
        onWait: (info) => waits.push(info), ...deps,
      });
    },
    options(patch = {}) {
      return { apiKey: "synthetic-mimo-key", fetch, maxRetries: 0, ...patch };
    },
  };
}

test("mimo native: adapter version is exactly 0.85.1", () => {
  assert.equal(JSON.parse(readFileSync(path.join(PI_CODING_AGENT_ROOT, "package.json"))).version, "0.85.1");
});

for (const id of ["mimo-v2.6", "mimo-v2.6-pro", "mimo-v2.6-flash"]) {
  for (const maxRetries of [0, 2]) {
    test(`mimo native: ${id} waits 210/241s with maxRetries=${maxRetries}`, { timeout: 3000 }, async () => {
      const f = await fixture([eligible503({ retryAfter: 210 }), eligible503({ retryAfter: 241 }), sse200()]);
      try {
        // Native retry would reject this delay, proving it cannot own the wait.
        const events = await drain(f.stream({ maxWaitMs: 451002 })(model({ id }), context(), f.options({ maxRetries, maxRetryDelayMs: 1 })));
        assert.deepEqual(f.delays, [210001, 241001]);
        assert.equal(f.proxy.requests.length, 3);
        assert.equal(events.filter(e => e.type === "error").length, 0);
        assert.equal(events.at(-1).type, "done");
        assert.ok(f.waits.some(w => w?.retryAfterMs === 241000 && w.waitedMs === 210001));
        assert.equal(f.waits.at(-1), null);
        for (const req of f.proxy.requests) {
          assert.equal(req.method, "POST");
          assert.equal(req.url, "/v1/chat/completions");
          assert.equal(req.body, f.proxy.requests[0].body);
        }
        assert.ok(f.originals.every(r => r.headers.get("x-should-retry") === null));
      } finally { await f.proxy.close(); }
    });
  }
}

for (const [label, deps, expectedCalls, expectedDelays, message] of [
  ["first wait exceeds budget", { maxWaitMs: 210000 }, 1, [], /admission wait budget exhausted/],
  ["cumulative waits exceed budget", { maxWaitMs: 420001 }, 2, [210001], /210001 ms of waiting/],
  ["rejection ceiling", { maxRejections: 2 }, 3, [210001, 210001], /gave up after 3/],
]) {
  test(`mimo native: bounded ${label}`, { timeout: 3000 }, async () => {
    const f = await fixture([eligible503({ retryAfter: 210 })]);
    try {
      const events = await drain(f.stream(deps)(model(), context(), f.options({ maxRetries: 3, maxRetryDelayMs: 1 })));
      assert.equal(f.proxy.requests.length, expectedCalls);
      assert.deepEqual(f.delays, expectedDelays);
      assert.equal(events.length, 1);
      assert.equal(events[0].reason, "error");
      assert.match(events[0].error.errorMessage, message);
      assert.doesNotMatch(events[0].error.errorMessage, /503|rate limit|proxy queue wait exceeded/);
      assert.equal(f.waits.at(-1), null);
    } finally { await f.proxy.close(); }
  });
}

const responseNegatives = [
  ["missing marker", r => { delete r.headers[MARKER_HEADER]; }],
  ["missing queue stamp", r => { delete r.headers[QUEUE_TIMEOUT_HEADER]; }],
  ["wrong marker", r => { r.headers[MARKER_HEADER] = "true"; }],
  ["wrong queue stamp", r => { r.headers[QUEUE_TIMEOUT_HEADER] = "01"; }],
  ["missing newline", r => { r.body = r.body.trimEnd(); }],
  ["extra newline", r => { r.body += "\n"; }],
  ["body prefix", r => { r.body = r.body.slice(0, 40); }],
  ["wrong body", r => { r.body = "upstream unavailable\n"; }],
  ...[401, 403, 429, 500, 529].map(status => [`status ${status}`, r => { r.status = status; }]),
];
for (const [label, mutate] of responseNegatives) {
  test(`mimo native: ineligible ${label} passes through`, { timeout: 3000 }, async () => {
    const response = eligible503({ retryAfter: 0.001 });
    mutate(response);
    const f = await fixture([response]);
    try {
      const events = await drain(f.stream()(model(), context(), f.options()));
      assert.equal(f.proxy.requests.length, 1);
      assert.deepEqual(f.delays, []);
      assert.equal(events.length, 1);
      assert.equal(events[0].error.errorMessage, `${response.status} ${response.body}`);
    } finally { await f.proxy.close(); }
  });
}

for (const [label, patch] of [
  ["foreign provider", { provider: "xiaomi" }],
  ["other desktop provider", { provider: "mimo-other" }],
  ["non-MiMo model", { id: "gpt-fixture" }],
  ["foreign host", { baseUrl: "http://example.invalid:8773/v1" }],
  ["https", { baseUrl: "https://127.0.0.1:8773/v1" }],
  ["wrong port", { baseUrl: "http://127.0.0.1:8774/v1" }],
  ["ZAI cannot inherit MiMo lane", { provider: "zai", id: "glm-5.3" }],
  ["crossed ZAI lane", { baseUrl: "http://127.0.0.1:8766/api/coding/paas/v4" }],
  ["crossed ZAI path", { baseUrl: "http://127.0.0.1:8773/api/coding/paas/v4" }],
  ["path prefix", { baseUrl: "http://127.0.0.1:8773/other/v1" }],
  ["query", { baseUrl: `${BASE}?x=1` }],
  ["fragment", { baseUrl: `${BASE}#x` }],
  ["credentials", { baseUrl: "http://fixture:secret@127.0.0.1:8773/v1" }],
]) {
  test(`mimo native: ineligible ${label}`, { timeout: 3000 }, async () => {
    const f = await fixture([eligible503({ retryAfter: 0.001 })]);
    try {
      const events = await drain(f.stream()(model(patch), context(), f.options()));
      assert.equal(f.proxy.requests.length, 1);
      assert.deepEqual(f.delays, []);
      assert.equal(events.length, 1);
      assert.equal(events[0].error.errorMessage, `503 ${PROXY_QUEUE_BODY}`);
    } finally { await f.proxy.close(); }
  });
}

for (const [label, decorate] of [
  ["redirect", r => Object.defineProperty(r, "redirected", { value: true })],
  ["foreign final URL", r => Object.defineProperty(r, "url", { value: "http://example.invalid:8773/v1/chat/completions" })],
  ["final query", r => Object.defineProperty(r, "url", { value: `${r.url}?redirect=1` })],
]) {
  test(`mimo native: ${label} cannot lend provenance`, { timeout: 3000 }, async () => {
    const f = await fixture([eligible503()]);
    try {
      const events = await drain(f.stream()(model(), context(), f.options({ fetch: async (...args) => {
        const res = await f.fetch(...args); decorate(res); return res;
      } })));
      assert.equal(f.proxy.requests.length, 1);
      assert.deepEqual(f.delays, []);
      assert.equal(events[0].error.errorMessage, `503 ${PROXY_QUEUE_BODY}`);
    } finally { await f.proxy.close(); }
  });
}

for (const [label, first] of [
  ["raw503", { status: 503, headers: { "retry-after": "0.001" }, body: "unavailable" }],
  ["quota429", { status: 429, headers: { "retry-after": "0.001" }, body: "quota" }],
  ["wrong body", { ...eligible503({ retryAfter: 0.001 }), body: "not a queue rejection" }],
]) {
  test(`mimo native: unrelated ${label} keeps maxRetries=1`, { timeout: 3000 }, async () => {
    const f = await fixture([first, sse200()]);
    try {
      const events = await drain(f.stream()(model(), context(), f.options({ maxRetries: 1 })));
      assert.equal(f.proxy.requests.length, 2);
      assert.deepEqual(f.delays, []);
      assert.equal(events.at(-1).type, "done");
    } finally { await f.proxy.close(); }
  });
}

test("mimo native: cancellation during REAL admission sleep never redispatches", { timeout: 3000 }, async () => {
  const f = await fixture([eligible503({ retryAfter: 241 })]);
  const controller = new AbortController();
  try {
    const events = await drain(f.stream({ sleep: undefined, onWait(info) {
      f.waits.push(info);
      if (info) queueMicrotask(() => controller.abort());
    } })(model(), context(), f.options({ signal: controller.signal, maxRetries: 2, maxRetryDelayMs: 1 })));
    assert.ok(f.waits.some(w => w?.delayMs === 241001), "must reach the admission sleep, not abort the fetch");
    assert.equal(f.proxy.requests.length, 1);
    assert.equal(events.length, 1);
    assert.equal(events[0].reason, "aborted");
    assert.equal(f.waits.at(-1), null);
  } finally { controller.abort(); await f.proxy.close(); }
});

test("mimo native: stamped rejection has exactly zero usage and no earlier event", { timeout: 3000 }, async () => {
  const f = await fixture([eligible503()]);
  try {
    const events = await drain(f.pi.streamSimple(model(), context(), f.options()));
    assert.equal(events.length, 1);
    assert.equal(events[0].type, "error");
    assert.equal(events[0].reason, "error");
    assert.equal(events[0].error.stopReason, "error");
    assert.deepEqual(events[0].error.content, []);
    assert.deepEqual(events[0].error.usage, {
      input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    });
  } finally { await f.proxy.close(); }
});

// Native event fault-injection: the actual adapter still fetches/parses the
// rejection; only its event is altered to exercise future/uncertain shapes.
const eventNegatives = [
  ["content", e => { e.error.content = [{ type: "text", text: "partial" }]; }],
  ["reason", e => { e.reason = "stop"; }],
  ["stopReason", e => { e.error.stopReason = undefined; }],
  ...["input", "output", "cacheRead", "cacheWrite", "totalTokens"].flatMap(key =>
    [1, null, undefined, "0"].map(value => [`usage.${key}=${value}`, e => { e.error.usage[key] = value; }])),
  ...["input", "output", "cacheRead", "cacheWrite", "total"].flatMap(key =>
    [1, null, undefined, "0"].map(value => [`cost.${key}=${value}`, e => { e.error.usage.cost[key] = value; }])),
];
for (const [label, mutate] of eventNegatives) {
  test(`mimo native event guard: ${label}`, { timeout: 3000 }, async () => {
    const f = await fixture([eligible503({ retryAfter: 0.001 })]);
    let terminal;
    try {
      const events = await drain(f.stream({ streamSimple(m, c, o) {
        const out = f.pi.createEventStream();
        (async () => {
          for await (const e of f.pi.streamSimple(m, c, o)) {
            if (e.type === "error") { mutate(e); terminal = e; }
            out.push(e);
          }
          out.end();
        })();
        return out;
      } })(model(), context(), f.options({ maxRetries: 1 })));
      assert.equal(f.proxy.requests.length, 1);
      assert.deepEqual(f.delays, []);
      assert.equal(events[0], terminal, "pass through the exact original event object");
    } finally { await f.proxy.close(); }
  });
}

for (const [kind, delta] of [
  ["text", { content: "partial answer" }],
  ["thinking", { reasoning_content: "partial thought" }],
  ["toolcall", { tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "probe", arguments: "{}" } }] }],
]) {
  test(`mimo native: partial ${kind} stream is never replayed`, { timeout: 3000 }, async () => {
    const body = `data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason: null }] })}\n\n` +
      `data: ${JSON.stringify({ error: { message: `503 ${PROXY_QUEUE_BODY}` } })}\n\n`;
    const f = await fixture([{ status: 200, headers: { "content-type": "text/event-stream", [MARKER_HEADER]: "1", [QUEUE_TIMEOUT_HEADER]: "1" }, body }]);
    try {
      const events = await drain(f.stream()(model(), context(), f.options({ maxRetries: 2 })));
      assert.equal(f.proxy.requests.length, 1);
      assert.deepEqual(f.delays, []);
      assert.ok(events.some(e => e.type === `${kind}_delta`));
      assert.equal(events.at(-1).type, "error");
    } finally { await f.proxy.close(); }
  });
}

test("mimo native: caller options and wire serialization survive redispatch", { timeout: 3000 }, async () => {
  const f = await fixture([sse200(), eligible503({ retryAfter: 0.001 }), sse200()]);
  const controller = new AbortController();
  const m = model(), c = context(), delegated = [];
  let payloads = 0, responses = 0;
  const options = f.options({
    signal: controller.signal, timeoutMs: 1234, maxRetries: 2, maxRetryDelayMs: 10,
    temperature: 0.3, maxTokens: 71, sessionId: "synthetic-session", cacheRetention: "none",
    headers: { "x-fixture": "keep" }, env: { FIXTURE: "preserve" },
    onPayload(p) { payloads++; return { ...p, seed: 42 }; },
    onResponse() { responses++; },
  });
  try {
    await drain(f.pi.streamSimple(m, c, options));
    const events = await drain(f.stream({ streamSimple(actualModel, actualContext, actualOptions) {
      assert.equal(actualModel, m); assert.equal(actualContext, c);
      delegated.push(actualOptions);
      return f.pi.streamSimple(actualModel, actualContext, actualOptions);
    } })(m, c, options));
    assert.equal(events.at(-1).type, "done");
    assert.equal(delegated.length, 2);
    for (const actual of delegated) for (const key of Object.keys(options)) {
      if (key !== "fetch") assert.equal(actual[key], options[key], `preserve ${key}`);
    }
    assert.equal(options.fetch, f.fetch);
    assert.equal(payloads, 3); assert.equal(responses, 2);
    assert.deepEqual(f.proxy.requests[1], f.proxy.requests[0]);
    assert.deepEqual(f.proxy.requests[2], f.proxy.requests[0]);
  } finally { await f.proxy.close(); }
});

for (const host of ["localhost", "[::1]"]) {
  test(`mimo native: canonical loopback ${host}`, async () => {
    const f = await fixture([eligible503({ retryAfter: 0.001 }), sse200()]);
    try {
      const events = await drain(f.stream()(model({ baseUrl: `http://${host}:8773/v1` }), context(), f.options()));
      assert.equal(events.at(-1).type, "done");
      assert.deepEqual(f.delays, [2]);
    } finally { await f.proxy.close(); }
  });
}

test("mimo native: already aborted signal makes zero requests", async () => {
  const f = await fixture([eligible503()]);
  const controller = new AbortController();
  controller.abort();
  try {
    const events = await drain(f.stream()(model(), context(), f.options({ signal: controller.signal, maxRetries: 2 })));
    assert.equal(f.calls.length, 0);
    assert.equal(events.length, 1);
    assert.equal(events[0].reason, "aborted");
  } finally { await f.proxy.close(); }
});

test("mimo native event guard: prior start vetoes even an exact zero-usage rejection", async () => {
  const f = await fixture([eligible503({ retryAfter: 0.001 })]);
  let terminal;
  try {
    const events = await drain(f.stream({ streamSimple(m, c, o) {
      const out = f.pi.createEventStream();
      (async () => {
        for await (const e of f.pi.streamSimple(m, c, o)) {
          if (e.type === "error") {
            terminal = e;
            out.push({ type: "start", partial: e.error });
          }
          out.push(e);
        }
        out.end();
      })();
      return out;
    } })(model(), context(), f.options({ maxRetries: 1 })));
    assert.equal(f.proxy.requests.length, 1);
    assert.deepEqual(f.delays, []);
    assert.deepEqual(events.map(e => e.type), ["start", "error"]);
    assert.equal(events.at(-1), terminal);
  } finally { await f.proxy.close(); }
});

async function startExtension(loaded, runtime, statuses = []) {
  for (const extension of loaded.result.extensions) {
    for (const handler of extension.handlers.get("session_start") ?? []) {
      await handler({ reason: "startup" }, {
        modelRegistry: runtime, ui: { setStatus: (...args) => statuses.push(args) },
      });
    }
  }
}

test("mimo extension: absent MiMo configuration creates no provider or auth entry", async () => {
  const { ModelRuntime } = await loadModelRuntimeModule();
  const dir = mkdtempSync(path.join(tmpdir(), "mimo-absent-"));
  try {
    const runtime = await ModelRuntime.create({ authPath: path.join(dir, "auth.json"), modelsPath: null, refreshOnCreate: false });
    const beforeIds = runtime.getProviders().map(p => p.id).sort();
    const loaded = await loadQueueWaitExtensionRegistration();
    await startExtension(loaded, runtime);
    assert.deepEqual(loaded.registrations.map(r => r.name).sort(), ["deepseek", "zai"], "zai always, deepseek because it ships builtin");
    for (const r of loaded.registrations) runtime.registerProvider(r.name, r.config);
    assert.deepEqual(runtime.getProviders().map(p => p.id).sort(), beforeIds);
    assert.equal(runtime.getProvider("mimo-desktop"), undefined);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("mimo extension: real loader overlay preserves models.json configuration", async () => {
  const { ModelRuntime } = await loadModelRuntimeModule();
  const dir = mkdtempSync(path.join(tmpdir(), "mimo-registration-"));
  try {
    const modelsPath = path.join(dir, "models.json");
    writeFileSync(modelsPath, JSON.stringify({ providers: { "mimo-desktop": {
      baseUrl: BASE, api: "openai-completions", apiKey: "synthetic-config-key",
      headers: { "x-fixture": "configured" },
      models: [model(), model({ id: "mimo-v2.6-flash" })],
    } } }));
    const runtime = await ModelRuntime.create({ authPath: path.join(dir, "auth.json"), modelsPath, refreshOnCreate: false });
    const beforeIds = runtime.getProviders().map(p => p.id).sort();
    const before = structuredClone(runtime.getModels("mimo-desktop"));
    const authArgs = { ctx: { env: async () => undefined }, signal: new AbortController().signal };
    const beforeAuth = await runtime.getProvider("mimo-desktop").auth.apiKey.resolve(authArgs);
    const loaded = await loadQueueWaitExtensionRegistration();
    const statuses = [];
    await startExtension(loaded, runtime, statuses);
    const { registrations } = loaded;
    assert.deepEqual(registrations.map(r => r.name).sort(), ["deepseek", "mimo-desktop", "zai"]);
    for (const { config } of registrations) {
      assert.deepEqual(Object.keys(config).sort(), ["api", "streamSimple"]);
      assert.equal(config.api, "openai-completions");
      assert.equal(typeof config.streamSimple, "function");
    }
    for (const r of registrations) runtime.registerProvider(r.name, r.config);
    assert.deepEqual(runtime.getProviders().map(p => p.id).sort(), beforeIds);
    assert.deepEqual(runtime.getModels("mimo-desktop"), before);
    assert.equal(runtime.getProvider("mimo-desktop").baseUrl, BASE);
    assert.deepEqual(await runtime.getProvider("mimo-desktop").auth.apiKey.resolve(authArgs), beforeAuth);
    assert.equal(runtime.getRegisteredProviderConfig("mimo-desktop").streamSimple, registrations.find(r => r.name === "mimo-desktop").config.streamSimple);
    const f = await fixture([eligible503({ retryAfter: 0.01 }), sse200()]);
    try {
      const events = await drain(runtime.getProvider("mimo-desktop").streamSimple(
        runtime.getModel("mimo-desktop", "mimo-v2.6-pro"), context(),
        f.options({ maxRetries: 2, maxRetryDelayMs: 1 }),
      ));
      assert.equal(events.at(-1).type, "done", "actual runtime dispatch reaches the installed overlay");
      assert.equal(f.proxy.requests.length, 2);
      assert.ok(statuses.some(([key, text]) => key === "queue-wait" && text?.includes("waiting")));
      assert.deepEqual(statuses.at(-1), ["queue-wait", undefined]);
    } finally { await f.proxy.close(); }
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
