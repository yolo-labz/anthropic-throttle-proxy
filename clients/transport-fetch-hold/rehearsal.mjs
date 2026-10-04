#!/usr/bin/env node
// 308 — fetch-hold transport REHEARSAL (loopback, synthetic only).
//
// Verifies the eight properties of the reusable transport fetch-hold against
// the INSTALLED Anthropic SDK over a loopback HTTP server, with a synthetic
// key and synthetic payloads. ZERO real provider traffic; no provider/model/
// billing ids are changed anywhere; no native provider registry, no live
// extension loading. This is a SOURCE rehearsal — it authorizes NO live gate,
// client or 24h claim.
//
//   node clients/transport-fetch-hold/rehearsal.mjs            (exit 0 = all pass)
//   node clients/transport-fetch-hold/rehearsal.mjs --selftest-fail   (exit 1 demo)
//
// Check map:
//   1 first attempt allowed            5 cancellation under hold releases
//   2 gate closes BEFORE the retry     6 admitted SSE preserved byte-exact
//   3 zero wire sends until resume     7 negative control of resume
//   4 slow body closed before upload   8 failure exits code != 0

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

async function resolveSdk() {
  // The SDK must be the INSTALLED one: env override, then this tree, then the
  // bounded pi-runtime nesting (read-only discovery, no extension is loaded).
  if (process.env.ANTHROPIC_SDK_PATH) {
    return require.resolve(process.env.ANTHROPIC_SDK_PATH);
  }
  try {
    return require.resolve("@anthropic-ai/sdk");
  } catch {
    /* fall through to the bounded discovery below */
  }
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
        return createRequire(candidate).resolve("@anthropic-ai/sdk");
      }
    }
  }
  throw new Error("installed @anthropic-ai/sdk not found (set ANTHROPIC_SDK_PATH)");
}

function startLoopback() {
  const hits = { total: 0, messages: 0, upload: 0, stream: 0, ping: 0, uploadBytes: 0 };
  const plan = []; // per-POST /v1/messages response plan: 500 first, 200 later
  const server = http.createServer(async (req, res) => {
    hits.total += 1;
    const url = req.url || "/";
    if (url.startsWith("/v1/messages")) {
      hits.messages += 1;
      const status = plan.length ? plan.shift() : 200;
      if (status !== 200) {
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify({ type: "error", error: { type: "api_error", message: "synthetic" } }));
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
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
      resolve({ server, hits, plan, base: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

async function main() {
  const failMode = process.argv.includes("--selftest-fail");
  const sdkEntry = await resolveSdk();
  const { default: Anthropic } = await import(pathToFileURL(sdkEntry).href);
  const { server, hits, plan, base } = await startLoopback();
  const hold = createHoldFetch();
  const client = new Anthropic({
    apiKey: rehearsalAuth,
    baseURL: base,
    fetch: hold.fetch,
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

  let retryPromise; // the internal retry, parked under hold until resume()
  let sseBody = "";

  await record("1 first attempt allowed", async () => {
    plan.push(500); // first attempt: allowed onto the wire, answers with 500
    let reached = false;
    try {
      await client.beta.messages.create(PAYLOAD, { maxRetries: 0 });
    } catch {
      reached = true;
    }
    expect(reached, "first attempt should hit the wire and fail with the synthetic 500");
    expect(hits.messages === 1, `expected 1 wire send, saw ${hits.messages}`);
    return "wire send #1 observed, synthetic 500 delivered";
  });

  await record("6 admitted SSE preserved byte-exact", async () => {
    const response = await hold.fetch(`${base}/stream`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let first = true;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      sseBody += decoder.decode(value, { stream: true });
      if (first) {
        first = false;
        hold.hold(); // gate closes MID-STREAM of an admitted response
      }
    }
    sseBody += decoder.decode();
    expect(
      sseBody === "data: chunk-1\n\ndata: chunk-2\n\ndata: chunk-3\n\ndata: chunk-4\n\ndata: chunk-5\n\ndata: done\n\n",
      "admitted SSE body was altered or truncated under hold",
    );
    expect(hold.isHeld(), "gate should be closed after the mid-stream hold");
    return "5 chunks + done byte-exact while the gate closed mid-stream";
  });

  await record("2 gate closes BEFORE the internal retry", async () => {
    plan.push(200); // the parked retry will be served after resume
    const before = hits.messages;
    retryPromise = client.beta.messages.create(PAYLOAD, { maxRetries: 0 });
    await sleep(150);
    expect(hold.parked() === 1, `retry should be parked, parked=${hold.parked()}`);
    expect(hits.messages === before, "retry reached the wire before resume");
    return "retry parked at the fetch seam, no wire send";
  });

  await record("3 zero new wire sends until resume", async () => {
    const before = hits.total;
    await sleep(250);
    expect(hits.total === before, `wire moved under hold: ${before} -> ${hits.total}`);
    expect(hold.parked() === 1, "the retry must stay parked");
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
      await hold.fetch(`${base}/upload`, { method: "POST", body: slow });
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
    const parkedCall = hold.fetch(`${base}/ping`, { method: "POST", body: "buffered", signal: controller.signal });
    await sleep(50);
    expect(hold.parked() === 2, `expected 2 parked, saw ${hold.parked()}`);
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
    expect(hold.parked() === 1, "cancelled waiter must be cleaned up");
    return `cancelled under hold released in ${elapsed}ms`;
  });

  await record("7 negative control of resume", async () => {
    const before = hits.total;
    hold.resume(); // the ONLY opener
    const message = await retryPromise;
    expect(message && message.type === "message", "retry should complete after resume");
    expect(hits.total === before + 1, `resume should release exactly one send: ${before} -> ${hits.total}`);
    hold.resume(); // double resume: harmless no-op
    hold.hold();
    hold.resume();
    const pong = await hold.fetch(`${base}/ping`);
    expect(pong.status === 200 && hits.ping === 1, "post-resume traffic must pass through");
    expect(!hold.isHeld() && hold.parked() === 0, "gate must end open and empty");
    return "resume released exactly the parked retry; no wrongful gating";
  });

  if (failMode) {
    await record("8 selftest-fail (deliberate)", async () => {
      throw new Error("deliberate rehearsal failure — exit-code demonstration");
    });
  }

  server.close();
  const failed = results.filter((r) => !r.ok);
  console.log(
    `RESULT ${failMode ? "selftest-fail" : "rehearsal"}: ${results.length - failed.length}/${results.length} checks passed; wire sends=${hits.total}`,
  );
  if (failed.length > 0) {
    process.exitCode = 1; // check 8: any failure exits non-zero
    console.log("EXIT 1 (failure present)");
  } else {
    console.log("EXIT 0");
  }
}

await main();
