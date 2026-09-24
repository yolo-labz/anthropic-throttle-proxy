// index.ts — Pi extension: keep a stamped queue-timeout rejection of the
// local ZAI or MiMo lane pending and retry after the advised delay (227/243).
//
// Registers streamSimple overrides for "zai" and "mimo-desktop" only.
// No models/baseUrl/apiKey/headers are passed, so the configured auth,
// endpoint, models and request options of both providers are preserved
// untouched (documented merge semantics of pi.registerProvider).
//
// Import note: the native OpenAI-completions adapter is imported from the
// loader-safe public entry "@earendil-works/pi-ai/compat", which re-exports
// the same factory as the jiti-hostile "@earendil-works/pi-ai/api/openai-
// completions.lazy" subpath.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import { createQueueWaitStream } from "./queue-wait.mjs";
import { normalizeContextForProvider } from "./normalize-families.mjs";

type StatusUi = { setStatus(key: string, text: string | undefined): void };

export default function (pi: ExtensionAPI) {
  let ui: StatusUi | undefined;

  const native = openAICompletionsApi();
  // Family-neutral replay (spec 2435): the adapter turns a FOREIGN `thinking`
  // block into the assistant's own text when the target model sets
  // requiresThinkingAsText, so a cross-family replay would hand the previous
  // model's private reasoning to the new provider as conversation. Drop those
  // blocks first and leave everything the adapter can synthesize to the
  // adapter; a context with nothing foreign passes through untouched.
  const streamSimpleForFamily = (model, context, options) => {
    const normalized = normalizeContextForProvider(context?.messages, model?.provider);
    return normalized.changed
      ? native.streamSimple(model, { ...context, messages: normalized.messages }, options)
      : native.streamSimple(model, context, options);
  };
  const streamSimple = createQueueWaitStream({
    streamSimple: streamSimpleForFamily,
    createEventStream: createAssistantMessageEventStream,
    onWait: (info) => {
      try {
        if (!ui) return;
        if (!info) {
          ui.setStatus("queue-wait", undefined);
          return;
        }
        const waited = info.fallback ? " (no Retry-After; fallback)" : "";
        ui.setStatus(
          "queue-wait",
          `queue-wait: admission rejected (${info.attempt}); waiting ${(info.delayMs / 1000).toFixed(1)}s${waited}`,
        );
      } catch {
        // Status display is best-effort only; never fail the request path.
      }
    },
    // NOTE: deps.allowedBaseUrls is a DI-only test seam and is deliberately
    // NOT set here — production keeps the provider-specific loopback gates.
  });

  const register = (provider: string) =>
    pi.registerProvider(provider, { api: "openai-completions", streamSimple });

  pi.on("session_start", (_event, ctx) => {
    ui = ctx.ui;
    // MiMo is user-configured, not built in. Do not invent an empty provider
    // (or auth entry) on hosts without it; models.json is loaded by this point.
    if (ctx.modelRegistry.getProvider("mimo-desktop")) register("mimo-desktop");
    // DeepSeek ships in Pi's own catalog, so the overlay only needs {api,
    // streamSimple}: a measured fixture confirms all three builtin models and
    // the baseUrl survive registration. The registry guard keeps a host with no
    // DeepSeek at all from gaining an empty one.
    if (ctx.modelRegistry.getProvider("deepseek")) register("deepseek");
  });
  pi.on("agent_start", (_event, ctx) => {
    ui = ctx.ui;
  });

  register("zai");

  // Disabling is removal/reload of the extension only (no toggle commands):
  // unregistering a provider could erase another extension's merged overlay, so
  // this extension never exposes queue-wait-off / queue-wait-on commands.
}
