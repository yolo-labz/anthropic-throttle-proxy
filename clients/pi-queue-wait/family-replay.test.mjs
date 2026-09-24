// family-replay.test.mjs — spec 2435 acceptance: a cross-family replay must
// never hand the previous model's reasoning to the new provider.
//
// Runs the real Pi 0.85.1 adapter and the real extension loader over an
// ephemeral loopback server (never :8773, never a credential). Two layers:
//
//   1. unit — the pure normalizer in ./normalize-families.mjs;
//   2. integration — the streamSimple the extension actually REGISTERS for
//      `mimo-desktop`, called with a DeepSeek-authored tool context, asserting
//      the bytes that would go on the wire.
//
// The red control at the bottom pins the upstream behaviour this module exists
// to mask (measured 24/09/2026): pi's openai-completions adapter re-serializes a
// foreign `thinking` block as the assistant's own message text.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  loadPiAi, loadModelRuntimeModule, loadQueueWaitExtensionRegistration, createFakeProxy, sse200,
} from "./native-replay.mjs";
import { contextFamilies, normalizeContextForProvider, normalizesProvider, providerFamily } from "./normalize-families.mjs";

const FOREIGN_REASONING = "FOREIGN_REASONING_TEXT";
const TOOL_RESULT = "TOOL_RESULT_TEXT";

const foreignContext = () => ({
  systemPrompt: "Hermetic cross-family replay.",
  messages: [
    { role: "user", content: [{ type: "text", text: "run the probe" }], timestamp: 1 },
    {
      role: "assistant", provider: "openai-codex", model: "gpt-6-astra", timestamp: 2,
      api: "openai-completions", stopReason: "toolUse",
      usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      content: [
        { type: "thinking", thinking: FOREIGN_REASONING, thinkingSignature: "sig-from-openai" },
        { type: "toolCall", id: "call_1", name: "bash", arguments: { command: "echo hi" } },
      ],
    },
    {
      role: "toolResult", provider: "openai-codex", toolCallId: "call_1", toolName: "bash", timestamp: 3,
      content: [{ type: "text", text: TOOL_RESULT }],
    },
    { role: "user", content: [{ type: "text", text: "carry on" }], timestamp: 4 },
  ],
});

const deepseekModel = (baseUrl) => ({
  id: "deepseek-v4-flash", name: "DeepSeek fixture", provider: "deepseek",
  api: "openai-completions", baseUrl, reasoning: false, input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128000, maxTokens: 8192,
});

const mimoModel = (baseUrl) => ({
  id: "mimo-v2.6-pro", name: "MiMo fixture", provider: "mimo-desktop",
  api: "openai-completions", baseUrl, reasoning: true,
  compat: { requiresReasoningContentOnAssistantMessages: true, thinkingFormat: "deepseek" },
  input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 1000000, maxTokens: 8192,
});

async function drain(stream) {
  const events = [];
  for await (const event of stream) events.push(event);
  return events;
}

// ── 1. unit ────────────────────────────────────────────────────────────────

test("unit: the covered-provider set is the single source of truth the harness imports", () => {
  assert.equal(normalizesProvider("zai"), true);
  assert.equal(normalizesProvider("mimo-desktop"), true);
  assert.equal(normalizesProvider("deepseek"), true);
  assert.equal(normalizesProvider("openai-codex"), false, "an unwrapped target must stay fail-closed");
  assert.equal(normalizesProvider("codex-b"), false);
});

test("unit: providerFamily mirrors the harness classification", () => {
  assert.equal(providerFamily("deepseek"), "chinese-frontier");
  assert.equal(providerFamily("mimo-desktop"), "chinese-frontier");
  assert.equal(providerFamily("zai"), "chinese-frontier");
  assert.equal(providerFamily("openai-codex"), "openai");
  assert.equal(providerFamily("codex-b"), "openai");
  assert.equal(providerFamily("claude-bridge"), "anthropic");
  assert.equal(providerFamily("ollama"), null);
  assert.equal(providerFamily(undefined), null);
});

test("unit: a foreign thinking block is dropped and the tool pair survives", () => {
  const before = foreignContext();
  const { messages, changed, droppedBlocks, families } = normalizeContextForProvider(before.messages, "mimo-desktop");
  assert.equal(changed, true);
  assert.equal(droppedBlocks, 1, "exactly the one thinking block");
  assert.deepEqual(families, ["openai"], "the authoring family is openai");
  const assistant = messages.find((m) => m.role === "assistant");
  assert.equal(JSON.stringify(messages).includes(FOREIGN_REASONING), false, "reasoning text must not survive");
  assert.deepEqual(assistant.content.map((b) => b.type), ["toolCall"], "tool call kept");
  assert.deepEqual(assistant.content[0].arguments, { command: "echo hi" });
  assert.equal(JSON.stringify(messages).includes(TOOL_RESULT), true, "tool result kept");
  assert.equal(before.messages[1].content.length, 2, "input context is never mutated");
});

test("unit: a message emptied by the drop keeps a boundary marker instead of vanishing", () => {
  const messages = [
    { role: "user", content: [{ type: "text", text: "hi" }], timestamp: 1 },
    {
      role: "assistant", provider: "openai-codex", timestamp: 2,
      content: [{ type: "thinking", thinking: "private reasoning", thinkingSignature: "sig" }],
    },
  ];
  const { messages: out, changed, markers } = normalizeContextForProvider(messages, "zai");
  assert.equal(changed, true);
  assert.equal(markers, 1);
  assert.equal(out.length, 2, "the turn is not removed from the transcript");
  assert.deepEqual(out[1].content, [{ type: "text", text: "[context normalized from openai]" }]);
});

test("unit: same-family, untagged and non-assistant messages pass through untouched", () => {
  const messages = [
    { role: "assistant", provider: "zai", timestamp: 1, content: [{ type: "thinking", thinking: "own reasoning" }] },
    { role: "assistant", timestamp: 2, content: [{ type: "thinking", thinking: "untagged: not evidence of another family" }] },
    { role: "toolResult", provider: "openai-codex", toolCallId: "c", timestamp: 3, content: [{ type: "text", text: "result" }] },
  ];
  const { messages: out, changed, droppedBlocks } = normalizeContextForProvider(messages, "mimo-desktop");
  assert.equal(changed, false);
  assert.equal(droppedBlocks, 0);
  assert.deepEqual(out, messages, "byte-identical passthrough");
});

test("unit: normalization is idempotent and safe on a missing target family", () => {
  const first = normalizeContextForProvider(foreignContext().messages, "mimo-desktop");
  const second = normalizeContextForProvider(first.messages, "mimo-desktop");
  assert.equal(second.changed, false, "second pass changes nothing");
  assert.deepEqual(second.messages, first.messages);
  const unknown = normalizeContextForProvider(foreignContext().messages, "ollama");
  assert.equal(unknown.changed, false, "an unknown target family is not normalized on a guess");
  assert.deepEqual(contextFamilies(foreignContext().messages), ["openai"]);
});

// ── 2. integration: the streamSimple the extension registers ───────────────

test("integration: the registered wrapper strips foreign reasoning before it reaches the wire", async () => {
  const { ModelRuntime } = await loadModelRuntimeModule();
  const proxy = createFakeProxy([sse200(), sse200()]);
  const dir = mkdtempSync(path.join(tmpdir(), "family-replay-"));
  try {
    const baseUrl = await proxy.baseUrl("");
    const modelsPath = path.join(dir, "models.json");
    writeFileSync(modelsPath, JSON.stringify({
      providers: {
        "mimo-desktop": {
          baseUrl, api: "openai-completions", apiKey: "synthetic-config-key",
          models: [{ id: "mimo-v2.6-pro", name: "MiMo fixture", reasoning: true, input: ["text"], contextWindow: 1000000, maxTokens: 8192 }],
        },
      },
    }));
    const runtime = await ModelRuntime.create({ authPath: path.join(dir, "auth.json"), modelsPath, refreshOnCreate: false });
    const loaded = await loadQueueWaitExtensionRegistration();
    for (const extension of loaded.result.extensions) {
      for (const handler of extension.handlers.get("session_start") ?? []) {
        await handler({ reason: "startup" }, { modelRegistry: runtime, ui: { setStatus: () => {} } });
      }
    }
    const registered = loaded.registrations.find((r) => r.name === "mimo-desktop");
    assert.ok(registered, "the extension must register mimo-desktop when the catalog declares it");

    await drain(registered.config.streamSimple(
      mimoModel(baseUrl), foreignContext(),
      { apiKey: "synthetic-config-key", fetch: (a, b) => globalThis.fetch(a, b), maxRetries: 0 },
    ));
    assert.equal(proxy.requests.length, 1, "exactly one upstream request");
    const body = JSON.parse(proxy.requests[0].body);
    const assistant = body.messages.find((m) => m.role === "assistant");
    assert.ok(assistant, "the assistant turn is still present");
    assert.equal(JSON.stringify(body).includes(FOREIGN_REASONING), false, "foreign reasoning must not reach the wire");
    assert.equal(JSON.stringify(body).includes("sig-from-openai"), false, "foreign signature must not reach the wire");
    assert.equal(assistant.tool_calls?.[0]?.function?.name, "bash", "tool call survives the normalization");
    assert.equal(body.messages.some((m) => m.role === "tool"), true, "tool result still pairs with its call");
    assert.equal(JSON.stringify(body).includes(TOOL_RESULT), true, "tool result text survives");
  } finally {
    await proxy.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("integration: deepseek is registered and its requests are normalized too", async () => {
  const { ModelRuntime } = await loadModelRuntimeModule();
  const proxy = createFakeProxy([sse200()]);
  const dir = mkdtempSync(path.join(tmpdir(), "deepseek-family-"));
  try {
    const baseUrl = await proxy.baseUrl("");
    // A builtin provider only needs its endpoint redirected; the catalog entry
    // proves the overlay keeps the builtin models (measured separately).
    const modelsPath = path.join(dir, "models.json");
    writeFileSync(modelsPath, JSON.stringify({ providers: { deepseek: { baseUrl } } }));
    const runtime = await ModelRuntime.create({ authPath: path.join(dir, "auth.json"), modelsPath, refreshOnCreate: false });
    const builtinModels = runtime.getModels("deepseek").map((m) => m.id).sort();
    assert.ok(builtinModels.length > 0, "deepseek must exist as a builtin provider");
    const loaded = await loadQueueWaitExtensionRegistration();
    for (const extension of loaded.result.extensions) {
      for (const handler of extension.handlers.get("session_start") ?? []) {
        await handler({ reason: "startup" }, { modelRegistry: runtime, ui: { setStatus: () => {} } });
      }
    }
    const registered = loaded.registrations.find((r) => r.name === "deepseek");
    assert.ok(registered, "deepseek must be registered among the normalized providers");
    assert.deepEqual(runtime.getModels("deepseek").map((m) => m.id).sort(), builtinModels, "builtin catalog survives");

    await drain(registered.config.streamSimple(
      deepseekModel(baseUrl), foreignContext(),
      { apiKey: "synthetic-key", fetch: (a, b) => globalThis.fetch(a, b), maxRetries: 0 },
    ));
    const body = JSON.parse(proxy.requests[0].body);
    assert.equal(JSON.stringify(body).includes(FOREIGN_REASONING), false, "no foreign reasoning toward deepseek");
    assert.equal(body.messages.some((m) => m.role === "tool"), true, "tool pair survives");
  } finally {
    await proxy.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("integration control: the raw adapter alone DOES leak foreign reasoning as assistant text", async () => {
  const piAi = await loadPiAi();
  const proxy = createFakeProxy([sse200()]);
  try {
    const baseUrl = await proxy.baseUrl("");
    await drain(piAi.streamSimple(
      mimoModel(baseUrl), foreignContext(),
      { apiKey: "synthetic-config-key", fetch: (a, b) => globalThis.fetch(a, b), maxRetries: 0 },
    ));
    const body = JSON.parse(proxy.requests[0].body);
    const serialized = JSON.stringify(body);
    // If pi ever drops foreign thinking itself, this control flips to false and
    // the normalizer becomes belt-and-braces rather than load-bearing.
    assert.equal(serialized.includes(FOREIGN_REASONING), true, "upstream behaviour changed: re-measure spec 2435");
    const leaked = body.messages.find((m) => JSON.stringify(m).includes(FOREIGN_REASONING));
    assert.equal(leaked.role, "assistant");
    assert.equal(leaked.content.includes(FOREIGN_REASONING), true, "the leak is the assistant's own text");
  } finally {
    await proxy.close();
  }
});
