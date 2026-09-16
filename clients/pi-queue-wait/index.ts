// index.ts — Pi extension: keep a stamped queue-timeout rejection of the
// local z.ai lane pending and retry it after the advised delay (spec 227).
//
// Registers a streamSimple override for the EXISTING "zai" provider only.
// No models/baseUrl/apiKey/headers are passed, so the configured auth,
// endpoint, models and request options of the zai provider are preserved
// untouched (documented merge semantics of pi.registerProvider).
//
// Import note: the native OpenAI-completions adapter is imported from the
// loader-safe public entry "@earendil-works/pi-ai/compat", which re-exports
// the same factory as the jiti-hostile "@earendil-works/pi-ai/api/openai-
// completions.lazy" subpath.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import { createQueueWaitStream } from "./queue-wait.mjs";

type StatusUi = { setStatus(key: string, text: string | undefined): void };

export default function (pi: ExtensionAPI) {
  let ui: StatusUi | undefined;

  const native = openAICompletionsApi();
  const streamSimple = createQueueWaitStream({
    streamSimple: native.streamSimple,
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
    // NOT set here — production keeps the strict loopback :8766 gate.
  });

  const register = () => pi.registerProvider("zai", { api: "openai-completions", streamSimple });

  pi.on("session_start", (_event, ctx) => {
    ui = ctx.ui;
  });
  pi.on("agent_start", (_event, ctx) => {
    ui = ctx.ui;
  });

  register();

  // Disabling is removal/reload of the extension only (no toggle commands):
  // unregistering "zai" could erase another extension's merged overlay, so
  // this extension never exposes queue-wait-off / queue-wait-on commands.
}
