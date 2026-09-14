// native-replay.mjs — spec 227 test-author worker.
// Real Pi 0.84.4 adapter + fake loopback proxy + REAL extension loader.
// Node built-ins only. Imported by queue-wait.test.mjs (single validator entry:
//   node --test clients/pi-queue-wait/queue-wait.test.mjs
// ). No top-level pi-ai import: the unit suite must stay loadable without it.
//
// Gate amendments baked in (14/09/2026):
// - pi-ai is loaded via @earendil-works/pi-ai/compat (loader-safe; the jiti
//   alias mangles @earendil-works/pi-ai/api/openai-completions.lazy). compat
//   re-exports the api factory; call openAICompletionsApi() to get
//   {stream, streamSimple} (the lazy module has no direct streamSimple export).
// - index.ts is loaded through Pi's REAL extension loader
//   (dist/core/extensions/loader.js::loadExtensions + createExtensionRuntime),
//   never generic jiti, and the captured registration is applied to a real
//   ModelRuntime (dist/core/model-runtime.js).
// - Required native/registration tests FAIL (not skip) when Pi or index.ts
//   cannot load.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

// ---------------------------------------------------------------------------
// Proxy evidence — source-verified, not contract-pinned.
// src/anthropic_throttle_proxy/proxy.py::_queue_wait_timeout_response (~:891):
//   web.Response(status=503, headers={"retry-after": str(...),
//   config.QUEUE_TIMEOUT_HEADER: "1"}, text="proxy queue wait exceeded; slots
//   saturated — retrying will re-enter the fair queue\n")  — trailing newline.
// src/anthropic_throttle_proxy/config.py: MARKER_HEADER is stamped on every
// response this proxy serves.
// ---------------------------------------------------------------------------

export const MARKER_HEADER = "x-anthropic-throttle-proxy";
export const MARKER_VALUE = "1";
export const QUEUE_TIMEOUT_HEADER = "x-anthropic-throttle-queue-timeout";
export const QUEUE_TIMEOUT_VALUE = "1";
/** Exact body INCLUDING the trailing newline, source-verified at proxy.py:891. */
export const PROXY_QUEUE_BODY =
  "proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue\n";
export const DEFAULT_BASE_URL = "http://127.0.0.1:8766/api/coding/paas/v4";
export const CHAT_COMPLETIONS_PATH = "/api/coding/paas/v4/chat/completions";

/** Scripted eligible rejection, exactly as the real proxy emits it. */
export function eligible503({ retryAfter = 1 } = {}) {
  const headers = {
    [MARKER_HEADER]: MARKER_VALUE,
    [QUEUE_TIMEOUT_HEADER]: QUEUE_TIMEOUT_VALUE,
  };
  if (retryAfter !== undefined) headers["retry-after"] = String(retryAfter);
  return { status: 503, headers, body: PROXY_QUEUE_BODY };
}

const SSE_SUCCESS_BODY = [
  'data: {"id":"chatcmpl-queue-wait-test","object":"chat.completion.chunk","created":1,' +
    '"model":"glm-5.3-flash","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},' +
    '"finish_reason":null}]}',
  "",
  'data: {"id":"chatcmpl-queue-wait-test","object":"chat.completion.chunk","created":1,' +
    '"model":"glm-5.3-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],' +
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}',
  "",
  "data: [DONE]",
  "",
].join("\n");

/** Scripted successful chat-completions SSE response. */
export function sse200() {
  return {
    status: 200,
    headers: { "content-type": "text/event-stream" },
    body: SSE_SUCCESS_BODY,
  };
}

// ---------------------------------------------------------------------------
// Fake loopback proxy — plays a script of responses, records every request.
// ---------------------------------------------------------------------------

export function createFakeProxy(script) {
  const requests = [];
  const pending = new Set();
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      requests.push({
        method: req.method,
        url: req.url,
        headers: { ...req.headers },
        body: Buffer.concat(chunks).toString("utf8"),
      });
      const step = script.length > 1 ? script.shift() : script[0];
      const status = step.status ?? 200;
      const headers = { ...step.headers };
      const body = step.body ?? "";
      if (!("content-length" in headers)) headers["content-length"] = String(Buffer.byteLength(body));
      res.writeHead(status, headers);
      res.end(body);
      for (const waiter of pending) {
        if (requests.length < waiter.count) continue;
        clearTimeout(waiter.timer);
        pending.delete(waiter);
        waiter.resolve();
      }
    });
  });
  const listening = new Promise((resolve, reject) => {
    const onError = (error) => reject(error);
    server.once("error", onError);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", onError);
      resolve();
    });
  });
  return {
    requests,
    /** Resolves when the request count reaches n (poll-free wait). */
    waitForRequests(n, timeoutMs = 2_000) {
      if (requests.length >= n) return Promise.resolve();
      return new Promise((resolve, reject) => {
        const waiter = { count: n, resolve, reject, timer: undefined };
        waiter.timer = setTimeout(() => {
          pending.delete(waiter);
          reject(new Error(`Timed out after ${timeoutMs} ms waiting for ${n} loopback requests (saw ${requests.length})`));
        }, timeoutMs);
        pending.add(waiter);
      });
    },
    async baseUrl(suffix = "") {
      await listening;
      return `http://127.0.0.1:${server.address().port}/api/coding/paas/v4${suffix}`;
    },
    async close() {
      for (const waiter of pending) {
        clearTimeout(waiter.timer);
        waiter.reject(new Error("Fake loopback proxy closed before the expected request count"));
      }
      pending.clear();
      if (!server.listening) return;
      server.closeAllConnections?.();
      await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
    },
  };
}

// ---------------------------------------------------------------------------
// pi-ai loading (loader-safe @earendil-works/pi-ai/compat route).
// ---------------------------------------------------------------------------

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, "..", "..");
export const PI_CODING_AGENT_ROOT =
  process.env.PI_CODING_AGENT_ROOT ?? "/opt/pi/@earendil-works/pi-coding-agent";

let _piAi;
/**
 * Resolve + import @earendil-works/pi-ai/compat through the mounted Pi
 * package (its own node_modules chain), assert the factory surface the gate
 * pinned, and memoize. Throws (=> tests FAIL, never skip) when unloadable.
 */
export async function loadPiAi() {
  if (_piAi) return _piAi;
  let compatUrl;
  const reasons = [];
  for (const from of [path.join(PI_CODING_AGENT_ROOT, "package.json"), path.join(REPO_ROOT, "package.json")]) {
    try {
      const resolver = createRequire(from);
      let packageJsonPath;
      try {
        packageJsonPath = resolver.resolve("@earendil-works/pi-ai/package.json");
      } catch (error) {
        if (error?.code !== "ERR_PACKAGE_PATH_NOT_EXPORTED") throw error;
        for (const modulesDir of resolver.resolve.paths("@earendil-works/pi-ai") ?? []) {
          const candidate = path.join(modulesDir, "@earendil-works", "pi-ai", "package.json");
          try {
            const candidateManifest = JSON.parse(readFileSync(candidate, "utf8"));
            if (candidateManifest.name === "@earendil-works/pi-ai") {
              packageJsonPath = candidate;
              break;
            }
          } catch {
            // Keep walking the resolver's own node_modules search path.
          }
        }
        if (!packageJsonPath) throw error;
      }
      const manifest = JSON.parse(readFileSync(packageJsonPath, "utf8"));
      if (manifest.name !== "@earendil-works/pi-ai") throw new Error(`unexpected package manifest ${manifest.name}`);
      const compatImport = manifest.exports?.["./compat"]?.import;
      if (typeof compatImport !== "string" || compatImport === "") {
        throw new Error("package exports['./compat'].import is missing");
      }
      compatUrl = path.resolve(path.dirname(packageJsonPath), compatImport);
      break;
    } catch (error) {
      reasons.push(`${from}: ${error?.message ?? error}`);
    }
  }
  if (!compatUrl) {
    throw new Error(
      `Cannot resolve @earendil-works/pi-ai/compat (tried Pi mount and repo root):\n${reasons.join("\n")}`,
    );
  }
  const mod = await import(pathToFileURL(compatUrl).href);
  // Gate #11: the lazy module does not export streamSimple directly; compat
  // re-exports the factory — call it to get {stream, streamSimple}.
  const factory = mod.openAICompletionsApi;
  assert.equal(typeof factory, "function", "@earendil-works/pi-ai/compat must export openAICompletionsApi as a function");
  const api = factory();
  assert.ok(api && typeof api === "object", "openAICompletionsApi() must return an object");
  assert.equal(typeof api.streamSimple, "function", "openAICompletionsApi().streamSimple must be a function");
  assert.equal(typeof api.stream, "function", "openAICompletionsApi().stream must be a function");
  const createEventStream = mod.createAssistantMessageEventStream;
  assert.equal(typeof createEventStream, "function", "compat must export createAssistantMessageEventStream");
  _piAi = { mod, factory, api, streamSimple: api.streamSimple, createEventStream };
  return _piAi;
}

// ---------------------------------------------------------------------------
// Real Pi extension loader + real ModelRuntime.
// ---------------------------------------------------------------------------

let _loader;
export async function loadPiExtensionLoader() {
  if (_loader) return _loader;
  const loaderUrl = pathToFileURL(path.join(PI_CODING_AGENT_ROOT, "dist/core/extensions/loader.js"));
  _loader = await import(loaderUrl.href);
  assert.equal(
    typeof _loader.loadExtensions,
    "function",
    "Pi extension loader must export loadExtensions (dist/core/extensions/loader.js)",
  );
  assert.equal(
    typeof _loader.createExtensionRuntime,
    "function",
    "Pi extension loader must export createExtensionRuntime",
  );
  return _loader;
}

/**
 * Load clients/pi-queue-wait/index.ts through Pi's REAL extension loader and
 * return the captured provider registrations. During load, api.registerProvider
 * queues into runtime.pendingProviderRegistrations — that is the capture point.
 */
export async function loadQueueWaitExtensionRegistration(extensionPath = path.join(HERE, "index.ts")) {
  const loader = await loadPiExtensionLoader();
  const runtime = loader.createExtensionRuntime();
  const result = await loader.loadExtensions([extensionPath], path.dirname(extensionPath), undefined, runtime);
  const failure = result?.errors?.find((e) => e?.error);
  if (failure) {
    throw new Error(`Extension failed to load through the real Pi loader: ${failure.error?.stack ?? failure.error}`);
  }
  const registrations = runtime.pendingProviderRegistrations ?? [];
  return { runtime, result, registrations };
}

let _modelRuntimeModule;
export async function loadModelRuntimeModule() {
  if (_modelRuntimeModule) return _modelRuntimeModule;
  const url = pathToFileURL(path.join(PI_CODING_AGENT_ROOT, "dist/core/model-runtime.js"));
  _modelRuntimeModule = await import(url.href);
  assert.equal(typeof _modelRuntimeModule.ModelRuntime?.create, "function", "ModelRuntime.create must exist");
  return _modelRuntimeModule;
}

/**
 * Real ModelRuntime, hermetically configured: temp auth file (never the host
 * credential store), no user models.json, no network refresh.
 */
export async function createTestModelRuntime() {
  const { ModelRuntime } = await loadModelRuntimeModule();
  const dir = mkdtempSync(path.join(tmpdir(), "pi-queue-wait-test-"));
  try {
    const runtime = await ModelRuntime.create({
      authPath: path.join(dir, "auth.json"),
      modelsPath: null,
      refreshOnCreate: false,
    });
    return { runtime, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
  } catch (error) {
    rmSync(dir, { recursive: true, force: true });
    throw error;
  }
}

function modelSnapshot(runtime, providerId) {
  return structuredClone(runtime.getModels(providerId));
}

/**
 * Apply the captured registration to a REAL ModelRuntime and verify the
 * documented preservation contract.
 */
export async function runRegistrationPreservationChecks(registrations) {
  assert.equal(registrations.length, 1, `expected exactly one registerProvider call, saw ${registrations.length}`);
  const reg = registrations[0];
  assert.equal(reg.name, "zai", "extension must register provider 'zai' only");
  const config = reg.config ?? {};
  // Gate #4: api + streamSimple present; models/baseUrl/apiKey/headers omitted.
  assert.equal(config.api, "openai-completions", "registration must carry api (provider-composer rejects streamSimple without api)");
  assert.equal(typeof config.streamSimple, "function", "registration must carry a streamSimple function");
  for (const forbidden of ["models", "baseUrl", "apiKey", "headers"]) {
    assert.ok(
      !(forbidden in config) || config[forbidden] === undefined,
      `registration must omit ${forbidden} (saw ${JSON.stringify(config[forbidden])})`,
    );
  }

  const { runtime, cleanup } = await createTestModelRuntime();
  try {
    const beforeRegisteredIds = [...runtime.getRegisteredProviderIds()].sort();
    const beforeProviders = new Map(runtime.getProviders().map((provider) => [provider.id, provider]));
    const beforeModels = new Map([...beforeProviders.keys()].map((id) => [id, modelSnapshot(runtime, id)]));
    const beforeZai = beforeProviders.get("zai");
    const beforeGlm = runtime.getModel("zai", "glm-5.3-flash");
    assert.ok(beforeZai, "built-in zai provider must exist before extension registration");
    assert.ok(beforeGlm, "built-in zai/glm-5.3-flash model must exist before extension registration");

    runtime.registerProvider(reg.name, config);

    const afterRegisteredIds = [...runtime.getRegisteredProviderIds()].sort();
    assert.deepEqual(
      afterRegisteredIds,
      [...new Set([...beforeRegisteredIds, "zai"])].sort(),
      "registration registry may add only the zai overlay",
    );
    const afterProviders = new Map(runtime.getProviders().map((provider) => [provider.id, provider]));
    assert.deepEqual([...afterProviders.keys()].sort(), [...beforeProviders.keys()].sort(), "provider catalog IDs stay unchanged");
    for (const [id, provider] of beforeProviders) {
      assert.deepEqual(modelSnapshot(runtime, id), beforeModels.get(id), `provider ${id} models must be fully unchanged`);
      if (id !== "zai") {
        assert.equal(afterProviders.get(id), provider, `provider ${id} runtime object must be unchanged`);
      }
    }

    const afterZai = afterProviders.get("zai");
    const afterGlm = runtime.getModel("zai", "glm-5.3-flash");
    assert.equal(afterZai.id, beforeZai.id);
    assert.equal(afterZai.name, beforeZai.name);
    assert.equal(afterZai.baseUrl, beforeZai.baseUrl, "zai provider endpoint must be preserved");
    assert.deepEqual(afterZai.headers, beforeZai.headers, "zai provider headers must be preserved");
    assert.deepEqual(Object.keys(afterZai.auth).sort(), Object.keys(beforeZai.auth).sort(), "zai auth modes must be preserved");
    assert.equal(afterZai.auth.apiKey?.name, beforeZai.auth.apiKey?.name, "zai API-key auth identity must be preserved");
    assert.equal(afterZai.auth.apiKey?.login, beforeZai.auth.apiKey?.login, "zai API-key login must be preserved");
    assert.equal(typeof beforeZai.auth.apiKey?.resolve, "function");
    assert.equal(typeof afterZai.auth.apiKey?.resolve, "function");
    const authInput = {
      credential: { type: "api_key", key: "synthetic-registration-key" },
      ctx: { env: async () => undefined },
      signal: new AbortController().signal,
    };
    const beforeAuth = await beforeZai.auth.apiKey.resolve(authInput);
    const afterAuth = await afterZai.auth.apiKey.resolve(authInput);
    assert.equal(afterAuth.auth.apiKey, beforeAuth.auth.apiKey, "resolved API key must be preserved");
    assert.equal(afterAuth.auth.headers, beforeAuth.auth.headers, "resolved auth headers must be preserved");
    assert.deepEqual(afterAuth.env, beforeAuth.env, "resolved auth environment must be preserved");
    assert.equal(afterAuth.source, beforeAuth.source, "resolved auth source must be preserved");
    assert.deepEqual(afterGlm, beforeGlm, "zai model, endpoint, and all cost rates must be preserved exactly");

    const registeredConfig = runtime.getRegisteredProviderConfig("zai");
    assert.equal(registeredConfig.api, "openai-completions");
    assert.equal(registeredConfig.streamSimple, config.streamSimple, "the requested stream override must be installed exactly");

    runtime.unregisterProvider("zai");
    assert.deepEqual(
      [...runtime.getRegisteredProviderIds()].sort(),
      beforeRegisteredIds,
      "unregister must restore the registration registry exactly",
    );
    for (const [id, provider] of beforeProviders) {
      assert.equal(runtime.getProvider(id), provider, `provider ${id} must restore its original runtime object`);
      assert.deepEqual(modelSnapshot(runtime, id), beforeModels.get(id), `provider ${id} models must survive unregister`);
    }
    return { beforeRegisteredIds, afterRegisteredIds };
  } finally {
    cleanup();
  }
}

// ---------------------------------------------------------------------------
// Native replay tests (registered into node:test; called from the unit file).
// All sleeps are recorded + immediate so attempt counts stay deterministic;
// Retry-After values are kept tiny.
// ---------------------------------------------------------------------------

function recordingSleep() {
  const state = { delays: [], gate: null };
  const sleep = (ms, signal) => {
    state.delays.push(ms);
    if (state.gate) return state.gate.promise(ms, signal);
    if (signal?.aborted) return Promise.reject(new Error("aborted"));
    return Promise.resolve();
  };
  return { sleep, state };
}

/** Pass-through fetch observer: records method/url/body, delegates to globalThis.fetch. */
function observingFetch(log) {
  return async function fetch(input, init) {
    const url = typeof input === "string" ? input : input.url;
    log.push({ url, method: init?.method ?? "GET", headers: init?.headers, body: init?.body });
    return globalThis.fetch(input, init);
  };
}

async function drainEvents(stream) {
  let timer;
  const consume = (async () => {
    const events = [];
    for await (const event of stream) events.push(event);
    return events;
  })();
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("Native event stream timed out after 2000 ms")), 2_000);
  });
  return Promise.race([consume, timeout]).finally(() => clearTimeout(timer));
}

function makeModel(baseUrl, overrides = {}) {
  return {
    id: "glm-5.3-flash",
    name: "GLM-5.3-Flash fixture",
    provider: "zai",
    api: "openai-completions",
    baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 8192,
    ...overrides,
  };
}

function makeContext() {
  return {
    system: "You are a test.",
    messages: [{ role: "user", name: undefined, content: [{ type: "text", text: "PROBE-SECRET-BODY ping" }], timestamp: 0 }],
    tools: [],
  };
}

export function runNativeReplayTests() {
  test("native: marked queue rejection then success — no intermediate assistant error", async () => {
    const piAi = await loadPiAi();
    const proxy = createFakeProxy([eligible503({ retryAfter: 1 }), sse200()]);
    try {
      const base = await proxy.baseUrl();
      const { sleep, state } = recordingSleep();
      const waits = [];
      const fetchLog = [];
      const createQueueWaitStream = (await import("./queue-wait.mjs")).createQueueWaitStream;
      const stream = createQueueWaitStream({
        streamSimple: piAi.streamSimple,
        createEventStream: piAi.createEventStream,
        sleep,
        random: () => 0,
        onWait: (info) => waits.push(info),
        allowedBaseUrls: [base],
      })(
        makeModel(base),
        makeContext(),
        { apiKey: "sk-test-fake-key", fetch: observingFetch(fetchLog), maxRetries: 0 },
      );
      const events = await drainEvents(stream);
      await proxy.waitForRequests(2);
      assert.equal(proxy.requests.length, 2, `expected exactly 2 upstream attempts, saw ${proxy.requests.length}`);
      assert.equal(proxy.requests[0].method, "POST");
      assert.ok(proxy.requests[0].url.endsWith(CHAT_COMPLETIONS_PATH), `unexpected path ${proxy.requests[0].url}`);
      const errorEvents = events.filter((e) => e.type === "error");
      assert.equal(errorEvents.length, 0, `no intermediate assistant error expected, saw ${JSON.stringify(errorEvents)}`);
      const done = events.filter((e) => e.type === "done");
      assert.equal(done.length, 1, "exactly one done event expected");
      // Gate #5: random=0 ⇒ delay is exactly Retry-After + 1 ms of positive jitter.
      assert.deepEqual(state.delays, [1000 + 1], `expected [1001], saw ${JSON.stringify(state.delays)}`);
      assert.ok(waits.some((info) => info != null), "onWait must fire during admission wait");
      assert.equal(waits[waits.length - 1], null, "onWait must be cleared on the success path");
      assert.ok(fetchLog.length >= 2, "wrapper must observe via options.fetch");
    } finally {
      await proxy.close();
    }
  });

  test("native: request serialization identical with and without the wrapper", async () => {
    const piAi = await loadPiAi();
    const createQueueWaitStream = (await import("./queue-wait.mjs")).createQueueWaitStream;
    const proxy = createFakeProxy([sse200(), sse200()]);
    const context = makeContext();
    context.tools = [
      {
        name: "probe_tool",
        description: "probe",
        parameters: { type: "object", properties: { x: { type: "string" } }, required: ["x"] },
      },
    ];
    const runOne = async (wrapped, base, requestIndex) => {
      const fetchLog = [];
      const streamFn = wrapped
        ? createQueueWaitStream({
            streamSimple: piAi.streamSimple,
            createEventStream: piAi.createEventStream,
            sleep: () => Promise.resolve(),
            allowedBaseUrls: [base],
          })
        : piAi.streamSimple;
      const events = await drainEvents(
        streamFn(makeModel(base), context, {
          apiKey: "sk-test-fake-key",
          fetch: observingFetch(fetchLog),
          maxRetries: 0,
        }),
      );
      assert.equal(events.filter((e) => e.type === "error").length, 0);
      await proxy.waitForRequests(requestIndex + 1);
      return { proxy: proxy.requests[requestIndex], fetch: fetchLog[0] };
    };
    try {
      const base = await proxy.baseUrl();
      const plain = await runOne(false, base, 0);
      const wrappedRun = await runOne(true, base, 1);
      assert.deepEqual(
        wrappedRun.proxy,
        plain.proxy,
        "plain and wrapped calls to the same server must have exact method/path/host/headers/body equality",
      );
      assert.ok(wrappedRun.proxy.body.includes("probe_tool"), "tools must reach the wire unchanged");
      assert.ok(wrappedRun.fetch, "wrapped run must expose its fetch observation");
    } finally {
      await proxy.close();
    }
  });

  test("native: real adapter's 503 is one terminal error event, empty content, zero usage", async () => {
    const piAi = await loadPiAi();
    const proxy = createFakeProxy([eligible503({ retryAfter: 1 })]);
    try {
      const base = await proxy.baseUrl();
      const events = await drainEvents(
        piAi.streamSimple(makeModel(base), makeContext(), {
          apiKey: "sk-test-fake-key",
          fetch: (input, init) => globalThis.fetch(input, init),
          maxRetries: 0,
        }),
      );
      await proxy.waitForRequests(1);
      assert.equal(proxy.requests.length, 1, "native adapter must not retry on its own");
      const errors = events.filter((e) => e.type === "error");
      assert.equal(errors.length, 1, "exactly one error event");
      assert.equal(events[0].type, "error", "no event may precede the error (no start/content)");
      const err = errors[0].error ?? {};
      assert.equal(errors[0].reason, "error");
      assert.equal(err.role, "assistant");
      assert.deepEqual(err.content, []);
      assert.deepEqual(err.usage, {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      });
      assert.equal("cost" in err, false, "native AssistantMessage has no fictitious top-level cost");
      assert.equal(err.errorMessage, `503 ${PROXY_QUEUE_BODY}`, "native status and response text pass through exactly, including its trailing newline");
    } finally {
      await proxy.close();
    }
  });

  test("extension: real Pi loader captures one zai registration; ModelRuntime preserves configured providers", async () => {
    const { registrations } = await loadQueueWaitExtensionRegistration();
    await runRegistrationPreservationChecks(registrations);
  });

  test("native: wrong provider is a passthrough even against an eligible-looking 503", async () => {
    const piAi = await loadPiAi();
    const createQueueWaitStream = (await import("./queue-wait.mjs")).createQueueWaitStream;
    const proxy = createFakeProxy([eligible503({ retryAfter: 1 })]);
    try {
      const base = await proxy.baseUrl();
      const events = await drainEvents(
        createQueueWaitStream({
          streamSimple: piAi.streamSimple,
          createEventStream: piAi.createEventStream,
          sleep: () => Promise.resolve(),
          random: () => 0,
          allowedBaseUrls: [base],
        })(
          makeModel(base, { provider: "other" }),
          makeContext(),
          { apiKey: "sk-test-fake-key", maxRetries: 0 },
        ),
      );
      await proxy.waitForRequests(1);
      assert.equal(proxy.requests.length, 1, "wrong provider must never be retried");
      const errors = events.filter((e) => e.type === "error");
      assert.equal(errors.length, 1);
      assert.ok(String(errors[0].error?.errorMessage ?? "").includes("503"), "native error surfaces verbatim");
    } finally {
      await proxy.close();
    }
  });

  test("native: abort while parked in an admission wait", async () => {
    const piAi = await loadPiAi();
    const createQueueWaitStream = (await import("./queue-wait.mjs")).createQueueWaitStream;
    const proxy = createFakeProxy([eligible503({ retryAfter: 30 })]);
    try {
      const base = await proxy.baseUrl();
      const controller = new AbortController();
      let releaseSleep;
      const sleep = () =>
        new Promise((_, reject) => {
          releaseSleep = () => reject(new Error("aborted"));
          controller.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        });
      const waits = [];
      const stream = createQueueWaitStream({
        streamSimple: piAi.streamSimple,
        createEventStream: piAi.createEventStream,
        sleep,
        random: () => 0,
        onWait: (info) => waits.push(info),
        allowedBaseUrls: [base],
      })(makeModel(base), makeContext(), { apiKey: "sk-test-fake-key", signal: controller.signal, maxRetries: 0 });
      await proxy.waitForRequests(1);
      controller.abort();
      const events = await drainEvents(stream);
      assert.equal(proxy.requests.length, 1, "aborted wait must not produce another upstream call");
      const errors = events.filter((e) => e.type === "error");
      assert.equal(errors.length, 1);
      assert.equal(
        errors[0].reason === "aborted" || errors[0].error?.stopReason === "aborted",
        true,
        "terminal error must be the abort",
      );
      assert.equal(waits[waits.length - 1], null, "waiting UI cleared on abort");
    } finally {
      await proxy.close();
    }
  });
}
