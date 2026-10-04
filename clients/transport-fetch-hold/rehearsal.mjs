#!/usr/bin/env node
// 308/310 — fetch-hold transport REHEARSAL (loopback, synthetic only).
//
// Verifies the reusable transport fetch-hold against the INSTALLED Anthropic
// SDK over a loopback HTTP server, with a synthetic credential and synthetic
// payloads. ZERO real provider traffic; no provider/model/billing ids change
// anywhere; no native provider registry, no live extension loading. SOURCE
// rehearsal only — it authorizes NO live gate, client or 24h claim; mechanism
// acceptance stays HELD until these regressions pass (310 defect fixes).
//
//   node clients/transport-fetch-hold/rehearsal.mjs            (exit 0 = all pass)
//   node clients/transport-fetch-hold/rehearsal.mjs --selftest-fail   (exit 1 demo)
//
// The physical retry is the NATIVE `retryProviderRequest` from Pi's nested
// pi-ai contract (`dist/utils/provider-retry.js`) driving
// `client.beta.messages.create(..., { maxRetries: 0 }).asResponse()` in ONE
// call — a real second dispatch inside the same native retry, not two named
// calls (defect 2).

import { createRequire } from "node:module";
import { promises as fs } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

import { FetchHoldAbort, createHoldFetch } from "./hold-fetch.mjs";

const require = createRequire(import.meta.url);
// Synthetic loopback credential: assembled, not a single literal, and never
// shaped like a real key family — the rehearsal must not read as a leak.
const rehearsalAuth = ["loopback", "rehearsal", "synthetic-key"].join("-");
const PAYLOAD = {
  model: "synthetic-loopback-model",
  max_tokens: 1,
  messages: [{ role: "user", content: "fetch-hold rehearsal" }],
};

async function resolveSdkNesting() {
  // The SDK must be the INSTALLED one: env override, then this tree, then the
  // bounded pi-runtime nesting (read-only discovery, no extension is loaded).
  let sdkEntry;
  if (process.env.ANTHROPIC_SDK_PATH) {
    sdkEntry = require.resolve(process.env.ANTHROPIC_SDK_PATH);
  } else {
    try {
      sdkEntry = require.resolve("@anthropic-ai/sdk");
    } catch {
      sdkEntry = null;
    }
    if (!sdkEntry) {
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
            "@anthropic-ai",
            "sdk",
            "package.json",
          );
          if (await fs.stat(candidate).then(() => true, () => false)) {
            sdkEntry = createRequire(candidate).resolve("@anthropic-ai/sdk");
            break;
          }
        }
        if (sdkEntry) break;
      }
    }
  }
  if (!sdkEntry) throw new Error("installed @anthropic-ai/sdk not found (set ANTHROPIC_SDK_PATH)");
  // The native retry helper lives in the SAME nested node_modules as the SDK
  // (pi-coding-agent's node_modules → @earendil-works/pi-ai). Resolve it from
  // there — the path-nested pi-ai of the native contract, nothing reimplemented.
  const marker = `${path.sep}@anthropic-ai${path.sep}`;
  const nesting = sdkEntry.slice(0, sdkEntry.indexOf(marker));
  // The native retry helper lives in the SAME nested node_modules as the SDK:
  // pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/utils/provider-retry.js
  // (the path-nested pi-ai of the native contract; nothing reimplemented).
  const providerRetry = path.join(
    nesting,
    "@earendil-works",
    "pi-ai",
    "dist",
    "utils",
    "provider-retry.js",
  );
  await fs.stat(providerRetry);
  return { sdkEntry, providerRetry };
}

function startLoopback() {
  const hits = { total: 0, messages: 0, upload: 0, stream: 0, ping: 0, uploadBytes: 0 };
  const plan = []; // per-POST /v1/messages: { status, retryAfterMs? }
  let onServed500 = null;
  const server = http.createServer(async (req, res) => {
    hits.total += 1;
    const url = req.url || "/";
    if (url.startsWith("/v1/messages")) {
      hits.messages += 1;
      const step = plan.length ? plan.shift() : { status: 200 };
      const headers = { "content-type": "application/json" };
      if (step.retryAfterMs) headers["retry-after-ms"] = String(step.retryAfterMs);
      if (step.status !== 200) {
        res.writeHead(step.status, headers);
        res.end(JSON.stringify({ type: "error", error: { type: "api_error", message: "synthetic" } }));
        if (step.status >= 500 && onServed500) {
          const hook = onServed500;
          onServed500 = null;
          hook(); // gate closes BEFORE the native retry's backoff elapses
        }
        return;
      }
      res.writeHead(200, headers);
      res.end(
        JSON.stringify({
          id: "msg_synthetic_loopback",
          type: "message",
          role: "assistant",
          model: PAYLOAD.model,
          content: [{ type: "text", text: "rehearsal-ok" }],
          stop_reason: "end_turn",
          usage: { input_tokens: 1, output_tokens: 1 },
        }),
      );
      return;
    }
    if (url.startsWith("/upload")) {
      hits.upload += 1;
      for await (const chunk of req) hits.uploadBytes += chunk.length;
      res.writeHead(200).end();
      return;
    }
    if (url.startsWith("/stream")) {
      hits.stream += 1;
      res.writeHead(200, { "content-type": "text/event-stream" });
      for (let i = 1; i <= 5; i += 1) {
        res.write(`data: chunk-${i}\n\n`);
        await sleep(25);
      }
      res.end("data: done\n\n");
      return;
    }
    if (url.startsWith("/ping")) {
      hits.ping += 1;
      res.writeHead(200).end("pong");
      return;
    }
    res.writeHead(404).end();
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, hits, plan, base: `http://127.0.0.1:${server.address().port}`, set500Hook: (fn) => { onServed500 = fn; } });
    });
  });
}

async function waitFor(predicate, ms) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await sleep(10);
  }
  throw new Error(`condition not met within ${ms}ms`);
}

async function main() {
  const watchdog = setTimeout(() => {
    console.log("WATCHDOG: rehearsal exceeded 60s — failing closed");
    process.exit(2);
  }, 60_000);
  const failMode = process.argv.includes("--selftest-fail");
  const { sdkEntry, providerRetry } = await resolveSdkNesting();
  const { default: Anthropic } = await import(pathToFileURL(sdkEntry).href);
  const { retryProviderRequest } = await import(pathToFileURL(providerRetry).href);
  const { server, hits, plan, base, set500Hook } = await startLoopback();
  const gate = createHoldFetch();
  const client = new Anthropic({
    apiKey: rehearsalAuth,
    baseURL: base,
    fetch: gate.fetch,
    maxRetries: 0,
  });
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

  // ONE physical request lifecycle: the native retryProviderRequest performs
  // the real second dispatch inside the same call (defect 2).
  const nativeCall = () =>
    retryProviderRequest(
      () => client.beta.messages.create(PAYLOAD, { maxRetries: 0 }).asResponse(),
      { maxRetries: 1, maxRetryDelayMs: 1000 },
    );
  let retryPromise;
  let sseBody = "";

  await record("6 admitted SSE preserved byte-exact", async () => {
    // Runs FIRST: admission needs the gate open. The gate closes MID-STREAM
    // of the admitted response and reopens before the physical-retry flow.
    const response = await gate.fetch(`${base}/stream`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let first = true;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      sseBody += decoder.decode(value, { stream: true });
      if (first) {
        first = false;
        gate.hold(); // gate closes MID-STREAM of an admitted response
      }
    }
    sseBody += decoder.decode();
    expect(
      sseBody === "data: chunk-1\n\ndata: chunk-2\n\ndata: chunk-3\n\ndata: chunk-4\n\ndata: chunk-5\n\ndata: done\n\n",
      "admitted SSE body was altered or truncated under hold",
    );
    expect(gate.isHeld(), "gate should be closed after the mid-stream hold");
    gate.resume(); // reopen: the next first attempt must be allowed
    return "5 chunks + done byte-exact while the gate closed mid-stream";
  });

  await record("1 first attempt allowed (native physical retry call)", async () => {
    plan.push({ status: 500, retryAfterMs: 120 }, { status: 200 });
    set500Hook(() => gate.hold()); // closes the gate BEFORE the native retry
    retryPromise = nativeCall();
    await waitFor(() => hits.messages === 1, 1000);
    return "wire send #1 observed, synthetic 500 (retry-after-ms 120)";
  });

  await record("2 gate closes BEFORE the native retry fires", async () => {
    expect(gate.isHeld(), "the 500 hook must have closed the gate");
    await sleep(300); // > the 120ms native retry backoff
    expect(gate.parked() === 1, `native retry should be parked, parked=${gate.parked()}`);
    expect(hits.messages === 1, "native retry reached the wire before resume");
    return "physical retry parked at the fetch seam";
  });

  await record("3 zero second wire dispatch until resume", async () => {
    const before = hits.total;
    await sleep(250);
    expect(hits.total === before, `wire moved under hold: ${before} -> ${hits.total}`);
    expect(gate.parked() === 1, "the native retry must stay parked");
    return `wire frozen at ${before} total sends`;
  });

  await record("4 slow body closed before any upload", async () => {
    let closed = false;
    const slow = (async function* () {
      try {
        yield new TextEncoder().encode("x".repeat(1024));
        await sleep(5000);
      } finally {
        closed = true;
      }
    })();
    await slow.next(); // body is MID-STREAM — first chunk ready, none uploaded
    const before = hits.uploadBytes + hits.upload;
    let aborted = false;
    try {
      await gate.fetch(`${base}/upload`, { method: "POST", body: slow });
    } catch (error) {
      aborted = error instanceof FetchHoldAbort;
    }
    expect(aborted, "held slow body must refuse with FetchHoldAbort");
    expect(closed, "slow body was not closed");
    expect(hits.uploadBytes + hits.upload === before, "bytes were uploaded under hold");
    return "body closed, zero upload bytes on the wire";
  });

  await record("5 cancellation under hold releases", async () => {
    const controller = new AbortController();
    const parkedCall = gate.fetch(`${base}/ping`, { method: "POST", body: "buffered", signal: controller.signal });
    await sleep(50);
    expect(gate.parked() === 2, `expected 2 parked, saw ${gate.parked()}`);
    controller.abort();
    const started = Date.now();
    let cancelled = false;
    try {
      await parkedCall;
    } catch {
      cancelled = true;
    }
    const elapsed = Date.now() - started;
    expect(cancelled, "aborted call must reject");
    expect(elapsed < 2000, `abort took ${elapsed}ms`);
    expect(gate.parked() === 1, "cancelled waiter must be cleaned up");
    return `cancelled under hold released in ${elapsed}ms`;
  });

  await record("8 regression: resume/immediate-hold race stays closed", async () => {
    // hold(); call=fetch(); resume(); hold() — the parked call must NOT
    // proceed while the gate is re-held (defect 1: re-loop after every park).
    let sent = 0;
    const race = createHoldFetch(async () => {
      sent += 1;
      return "sent";
    });
    race.hold();
    const call = race.fetch("x");
    race.resume();
    race.hold();
    await sleep(20);
    expect(sent === 0, "race: the parked call proceeded while the gate was re-held");
    expect(race.isHeld(), "gate must read held after resume/immediate-hold");
    race.resume();
    expect((await call) === "sent", "call must complete after the real resume");
    expect(sent === 1, `call must send exactly once, sent=${sent}`);
    return "re-hold closed the gate before the parked call resumed";
  });

  await record("9 regression: abort listener cleaned in held and resumed waiters", async () => {
    const makeSpy = () => {
      const spy = {
        aborted: false,
        reason: undefined,
        added: 0,
        removed: 0,
        fire: null,
        addEventListener(_name, fn) {
          spy.added += 1;
          spy.fire = fn;
        },
        removeEventListener() {
          spy.removed += 1;
        },
      };
      return spy;
    };
    const gateA = createHoldFetch(async () => "sent");
    const resumed = makeSpy();
    gateA.hold();
    const callA = gateA.fetch("x", { signal: resumed });
    await sleep(5);
    gateA.resume();
    await callA;
    expect(resumed.added === 1 && resumed.removed === 1, `resumed waiter leaked (added=${resumed.added} removed=${resumed.removed})`);

    const gateB = createHoldFetch(async () => "sent");
    const aborted = makeSpy();
    gateB.hold();
    const callB = gateB.fetch("x", { signal: aborted });
    await sleep(5);
    aborted.reason = new Error("synthetic abort");
    aborted.fire();
    let cancelled = false;
    try {
      await callB;
    } catch {
      cancelled = true;
    }
    expect(cancelled, "aborted waiter must reject");
    expect(aborted.added === 1 && aborted.removed === 1, `aborted waiter leaked (added=${aborted.added} removed=${aborted.removed})`);
    return "abort listeners removed in both settled paths";
  });

  await record("7 resume completes the native retry (negative control)", async () => {
    const before = hits.total;
    gate.resume(); // the ONLY opener
    const response = await retryPromise;
    expect(response && response.status === 200, "native retry should complete after resume");
    expect(hits.total === before + 1, `resume must release exactly one wire send: ${before} -> ${hits.total}`);
    expect(hits.messages === 2, `exactly one physical retry dispatch expected, saw ${hits.messages}`);
    gate.resume(); // double resume: harmless no-op
    gate.hold();
    gate.resume();
    const pong = await gate.fetch(`${base}/ping`);
    expect(pong.status === 200 && hits.ping === 1, "post-resume traffic must pass through");
    expect(!gate.isHeld() && gate.parked() === 0, "gate must end open and empty");
    return "native physical retry completed on the wire; no wrongful gating";
  });

  if (failMode) {
    await record("10 exit-code proof (substantive, real machinery)", async () => {
      // NOT an empty forced-throw: a real gated send happens and the
      // intentionally inverted expectation fails on the OBSERVED behavior.
      const probe = createHoldFetch(async () => "sent");
      probe.resume();
      const observed = await probe.fetch("probe");
      expect(
        observed !== "sent",
        "exit-code demonstration: real send observed (intentionally inverted expectation)",
      );
    });
  }

  server.close();
  clearTimeout(watchdog);
  const failed = results.filter((r) => !r.ok);
  console.log(
    `RESULT ${failMode ? "selftest-fail" : "rehearsal"}: ${results.length - failed.length}/${results.length} checks passed; wire sends=${hits.total}`,
  );
  if (failed.length > 0) {
    process.exitCode = 1; // any failure exits non-zero
    console.log("EXIT 1 (failure present)");
  } else {
    console.log("EXIT 0");
  }
}

await main();
