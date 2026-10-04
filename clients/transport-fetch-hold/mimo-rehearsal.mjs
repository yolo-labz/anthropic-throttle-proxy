#!/usr/bin/env node
// 308→310→311 lineage — fetch-hold MiMo STREAM REHEARSAL (loopback, synthetic only).
//
// Hypothesis under test: the ACCEPTED fetch-hold (310 primitive, used here
// UNCHANGED) gates the REAL provider retry of the OpenAI-compatible stream
// path — the actual MiMo route — and preserves terminal SSE / tool / usage /
// cancel semantics. The 310 rehearsal exercised the ANTHROPIC SDK only; this
// run invokes the NATIVE installed openai-completions provider
// (`pi-ai/dist/api/openai-completions.js` — `options.fetch` seam at :186,
// native `retryProviderRequest` around `client.chat.completions.create(...).
// withResponse()` at :195-197) with local synthetic model/context/key and the
// REAL `options.fetch`. No retry/SSE implementation is copied; no provider
// registry, loader, billing or model-seat change. Xiaomi quota/window
// semantics are untouched: this is transport-level only, and every unknown
// stays unknown.
//
//   node clients/transport-fetch-hold/mimo-rehearsal.mjs           (exit 0)
//   node clients/transport-fetch-hold/mimo-rehearsal.mjs --selftest-fail
//
// Checks:
//   1 first 500 -> gate held BEFORE the native 2nd attempt of the SAME call
//     -> zero wire until resume -> stream terminal/tool/usage valid
//   2 already-admitted stream preserves bytes + terminal under closure
//   3a cancellation under hold does not invent completion
//   3b slow upload closed before upload (no completion possible)
//   3c uncertain send (mid-stream destroy) does not invent completion
//   4 substantive falsifier with exit nonzero (fail mode)

import { promises as fs } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

import { FetchHoldAbort, createHoldFetch } from "./hold-fetch.mjs";

// Synthetic loopback credential: assembled, not a single literal, and never
// shaped like a real key family — the rehearsal must not read as a leak.
const rehearsalAuth = ["loopback", "rehearsal", "synthetic-key"].join("-");

async function discoverProvider() {
  if (process.env.PI_AI_OPENAI_COMPLETIONS) return process.env.PI_AI_OPENAI_COMPLETIONS;
  const root = path.join(os.homedir(), ".cache", "pi-npx");
  const versions = (await fs.readdir(root).catch(() => [])).sort().reverse();
  for (const version of versions) {
    const npx = path.join(root, version, "_npx");
    for (const hash of await fs.readdir(npx).catch(() => [])) {
      const candidate = path.join(
        npx,
        hash,
        "node_modules",
        "@earendil-works",
        "pi-coding-agent",
        "node_modules",
        "@earendil-works",
        "pi-ai",
        "dist",
        "api",
        "openai-completions.js",
      );
      if (await fs.stat(candidate).then(() => true, () => false)) return candidate;
    }
  }
  throw new Error("installed pi-ai openai-completions not found (set PI_AI_OPENAI_COMPLETIONS)");
}

const FULL_SSE = [
  { choices: [{ index: 0, delta: { content: "mimo-" }, finish_reason: null }] },
  { choices: [{ index: 0, delta: { content: "stream-ok" }, finish_reason: null }] },
  {
    choices: [
      {
        index: 0,
        delta: {
          tool_calls: [
            { index: 0, id: "call_1", function: { name: "lookup", arguments: '{"q":' } },
          ],
        },
        finish_reason: null,
      },
    ],
  },
  {
    choices: [
      {
        index: 0,
        delta: { tool_calls: [{ index: 0, function: { arguments: ' "1"}' } }] },
        finish_reason: null,
      },
    ],
  },
  { choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }] },
  { choices: [], usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } },
];

const SLOW_SSE = [
  { choices: [{ index: 0, delta: { content: "alpha" }, finish_reason: null }] },
  { choices: [{ index: 0, delta: { content: "-beta" }, finish_reason: null }] },
  { choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
  { choices: [], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
];

function chunk(obj) {
  return `data: ${JSON.stringify({ id: "chatcmpl-synthetic", object: "chat.completion.chunk", model: "synthetic-loopback-model", ...obj })}\n\n`;
}

function startLoopback() {
  const hits = { total: 0, chat: 0, upload: 0, uploadBytes: 0 };
  const plan = []; // { status, retryAfterMs?, sse?: "full"|"slow"|"destroy" }
  let onServed500 = null;
  const server = http.createServer(async (req, res) => {
    hits.total += 1;
    const url = req.url || "/";
    if (!url.startsWith("/v1/chat/completions")) {
      res.writeHead(404).end();
      return;
    }
    hits.chat += 1;
    const step = plan.length ? plan.shift() : { status: 200, sse: "full" };
    const headers = { "content-type": "text/event-stream" };
    if (step.retryAfterMs) headers["retry-after-ms"] = String(step.retryAfterMs);
    if (step.status !== 200) {
      res.writeHead(step.status, { "content-type": "application/json", ...headers });
      res.end(JSON.stringify({ error: { message: "synthetic", type: "server_error" } }));
      if (step.status >= 500 && onServed500) {
        const hook = onServed500;
        onServed500 = null;
        hook(); // gate closes BEFORE the native retry's backoff elapses
      }
      return;
    }
    if (req.method === "POST") {
      for await (const part of req) hits.uploadBytes += part.length;
    }
    if (step.sse === "destroy") {
      res.writeHead(200, headers);
      res.write(chunk({ choices: [{ index: 0, delta: { content: "part" }, finish_reason: null }] }));
      res.write(chunk({ choices: [{ index: 0, delta: { content: "-ial" }, finish_reason: null }] }));
      res.destroy(); // uncertain send: the wire dies mid-stream
      return;
    }
    res.writeHead(200, headers);
    const frames = step.sse === "slow" ? SLOW_SSE : FULL_SSE;
    for (const frame of frames) {
      res.write(chunk(frame));
      await sleep(step.sse === "slow" ? 40 : 5);
    }
    res.end("data: [DONE]\n\n");
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({
        server,
        hits,
        plan,
        base: `http://127.0.0.1:${server.address().port}`,
        set500Hook: (fn) => {
          onServed500 = fn;
        },
      });
    });
  });
}

// Stable projection of provider events: the events carry a live `partial`
// object, so snapshot the SEMANTICS (bytes + terminal) at receive time.
function project(ev) {
  const out = { type: ev.type };
  if (ev.type === "text_end" || ev.type === "text_delta") out.content = ev.content ?? ev.delta;
  if (ev.type === "toolcall_end") out.tool = { name: ev.toolCall?.name, arguments: ev.toolCall?.arguments };
  if (ev.type === "done" || ev.type === "error") {
    out.reason = ev.reason;
    out.errorMessage = ev.message?.errorMessage ?? ev.error?.errorMessage ?? null;
    const message = ev.message ?? ev.error;
    out.text = message?.content?.filter((b) => b.type === "text").map((b) => b.text).join("") ?? "";
    out.tool = message?.content?.filter((b) => b.type === "toolCall").map((b) => ({ name: b.name, arguments: b.arguments })) ?? [];
    out.usage = message ? { input: message.usage?.input, output: message.usage?.output, totalTokens: message.usage?.totalTokens } : null;
    out.stopReason = message?.stopReason;
  }
  return out;
}

async function main() {
  const watchdog = setTimeout(() => {
    console.log("WATCHDOG: rehearsal exceeded 60s — failing closed");
    process.exit(2);
  }, 60_000);
  const failMode = process.argv.includes("--selftest-fail");
  const providerEntry = await discoverProvider();
  const { stream } = await import(pathToFileURL(providerEntry).href);
  const { server, hits, plan, base, set500Hook } = await startLoopback();
  const gate = createHoldFetch();

  // Local synthetic model/context/key — no registry, no real seat.
  const model = {
    api: "openai-completions",
    provider: "mimo-loopback",
    id: "synthetic-loopback-model",
    baseUrl: `${base}/v1`,
    maxTokens: 256,
    input: ["text"],
    output: ["text"],
    cost: { tiers: [] }, // synthetic: zero-cost, no billing ids
    headers: {},
    compat: { supportsUsageInStreaming: true, supportsFinishReason: true, maxTokensField: "max_tokens" },
  };
  const context = {
    messages: [{ role: "user", content: [{ type: "text", text: "mimo fetch-hold rehearsal" }] }],
  };
  const runStream = async (extra = {}) => {
    const events = [];
    for await (const ev of stream(model, context, { apiKey: rehearsalAuth, fetch: gate.fetch, maxRetries: 1, maxRetryDelayMs: 1000, ...extra })) {
      events.push(project(ev));
    }
    return events;
  };

  const results = [];
  const record = async (name, run) => {
    try {
      const detail = await run();
      results.push({ name, ok: true, detail: detail ?? "" });
      console.log(`PASS ${name}${detail ? ` — ${detail}` : ""}`);
    } catch (error) {
      results.push({ name, ok: false, detail: String(error && error.message ? error.message : error) });
      console.log(`FAIL ${name} — ${error}`);
    }
  };
  const expect = (cond, message) => {
    if (!cond) throw new Error(message);
  };
  const waitFor = async (predicate, ms) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      if (predicate()) return;
      await sleep(10);
    }
    throw new Error(`condition not met within ${ms}ms`);
  };

  let consuming;
  await record("1 native MiMo retry gated: hold before 2nd attempt, terminal valid", async () => {
    plan.length = 0; // each scenario owns its plan (an aborted call never drains one)
    plan.push({ status: 500, retryAfterMs: 120 }, { status: 200, sse: "full" });
    set500Hook(() => gate.hold());
    consuming = runStream(); // ONE call; the native retry re-enters inside it
    await waitFor(() => hits.chat === 1, 1500);
    expect(gate.isHeld(), "the 500 hook must have closed the gate");
    await sleep(300); // > the 120ms native retry backoff
    expect(gate.parked() === 1, `native retry should be parked, parked=${gate.parked()}`);
    expect(hits.chat === 1, "native retry reached the wire before resume");
    await sleep(200);
    expect(hits.chat === 1, "zero wire sends until resume");
    gate.resume();
    const events = await consuming;
    const done = events.find((e) => e.type === "done");
    expect(done, `no terminal done event: ${JSON.stringify(events.map((e) => e.type))}`);
    expect(events[events.length - 1].type === "done", "done must be the terminal event");
    expect(done.reason === "toolUse", `terminal reason should be toolUse, got ${done.reason}`);
    expect(done.stopReason === "toolUse", `stopReason should be toolUse, got ${done.stopReason}`);
    expect(done.text === "mimo-stream-ok", `text bytes altered: ${JSON.stringify(done.text)}`);
    expect(
      JSON.stringify(done.tool) === JSON.stringify([{ name: "lookup", arguments: { q: "1" } }]),
      `tool block invalid: ${JSON.stringify(done.tool)}`,
    );
    expect(
      JSON.stringify(done.usage) === JSON.stringify({ input: 3, output: 2, totalTokens: 5 }),
      `usage invalid: ${JSON.stringify(done.usage)}`,
    );
    expect(hits.chat === 2, `exactly one physical retry expected, chat=${hits.chat}`);
    return "500 -> held -> zero wire -> resume -> toolUse terminal (tool+usage valid)";
  });

  await record("2 admitted MiMo stream preserved byte/terminal under closure", async () => {
    plan.length = 0;
    plan.push({ status: 200, sse: "slow" }, { status: 200, sse: "slow" });
    const baseline = await runStream(); // gate open: the reference run
    expect(baseline.some((e) => e.type === "done"), "baseline must complete");
    let closed = false;
    const held = (async () => {
      const events = [];
      for await (const ev of stream(model, context, { apiKey: rehearsalAuth, fetch: gate.fetch, maxRetries: 1, maxRetryDelayMs: 1000 })) {
        events.push(project(ev));
        if (!closed) {
          closed = true;
          gate.hold(); // closure lands MID-STREAM of an admitted call
        }
      }
      return events;
    })();
    const heldEvents = await held;
    gate.resume();
    expect(
      JSON.stringify(heldEvents) === JSON.stringify(baseline),
      "admitted stream changed under closure",
    );
    return "event bytes + terminal identical to the open-gate baseline";
  });

  await record("3a cancellation under hold does not invent completion", async () => {
    plan.length = 0;
    plan.push({ status: 200, sse: "full" });
    gate.hold();
    const controller = new AbortController();
    const events = [];
    const consuming = (async () => {
      for await (const ev of stream(model, context, { apiKey: rehearsalAuth, fetch: gate.fetch, maxRetries: 1, maxRetryDelayMs: 1000, signal: controller.signal })) {
        events.push(project(ev));
      }
    })();
    await sleep(50);
    expect(gate.parked() === 1, "the create should be parked under hold");
    controller.abort();
    await consuming;
    gate.resume();
    expect(!events.some((e) => e.type === "done"), "cancellation invented a completion");
    const terminal = events[events.length - 1];
    expect(terminal && terminal.type === "error" && terminal.reason === "aborted", `expected aborted error terminal, got ${JSON.stringify(terminal)}`);
    return "aborted under hold -> error terminal (aborted), no done";
  });

  await record("3b slow upload closed before upload (no completion possible)", async () => {
    let closed = false;
    const slow = (async function* () {
      try {
        yield new TextEncoder().encode("x".repeat(1024));
        await sleep(5000);
      } finally {
        closed = true;
      }
    })();
    await slow.next();
    gate.hold();
    const before = hits.uploadBytes;
    let aborted = false;
    try {
      await gate.fetch(`${base}/v1/chat/completions`, { method: "POST", body: slow });
    } catch (error) {
      aborted = error instanceof FetchHoldAbort;
    }
    gate.resume();
    expect(aborted, "held slow body must refuse with FetchHoldAbort");
    expect(closed, "slow body was not closed");
    expect(hits.uploadBytes === before, "bytes were uploaded under hold");
    return "body closed pre-upload, zero bytes, no completion to invent";
  });

  await record("3c uncertain send does not invent completion", async () => {
    plan.length = 0;
    // BOTH attempts die mid-stream: the native retry is policy-owned and may
    // re-send, but an outcome that is uncertain EVERYWHERE must end honest
    // (error), never as an invented completion.
    plan.push({ status: 200, sse: "destroy" }, { status: 200, sse: "destroy" });
    const chatBefore = hits.chat;
    const events = await runStream();
    expect(hits.chat === chatBefore + 2, `expected 2 real uncertain sends, saw ${hits.chat - chatBefore}`);
    expect(!events.some((e) => e.type === "done"), "uncertain send invented a completion");
    const terminal = events[events.length - 1];
    expect(terminal && terminal.type === "error", `expected error terminal, got ${JSON.stringify(terminal)}`);
    expect(terminal.reason === "error", `expected reason error, got ${terminal.reason}`);
    return "mid-stream destroy -> error terminal, honest unknown";
  });

  if (failMode) {
    await record("4 falsifier (substantive, real MiMo machinery)", async () => {
      // NOT an empty forced-throw: a real MiMo stream completes and the
      // intentionally inverted terminal expectation fails on OBSERVED behavior.
      plan.length = 0;
      plan.push({ status: 200, sse: "full" });
      const events = await runStream();
      expect(
        !events.some((e) => e.type === "done"),
        "falsifier demonstration: real terminal done observed (intentionally inverted expectation)",
      );
    });
  }

  server.close();
  clearTimeout(watchdog);
  const failed = results.filter((r) => !r.ok);
  console.log(
    `RESULT ${failMode ? "selftest-fail" : "mimo-rehearsal"}: ${results.length - failed.length}/${results.length} checks passed; chat wire sends=${hits.chat}`,
  );
  if (failed.length > 0) {
    process.exitCode = 1;
    console.log("EXIT 1 (failure present)");
  } else {
    console.log("EXIT 0");
  }
}

await main();
