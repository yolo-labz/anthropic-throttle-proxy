// normalize-families.mjs — replay a session across provider families without
// handing the previous model's private reasoning to the new provider.
//
// Why this exists (measured 24/09/2026, NixOS spec 2435, real adapter through
// this repo's own native-replay rig): pi's openai-completions adapter does NOT
// drop a foreign family's `thinking` block. With `requiresThinkingAsText` it
// re-serializes that reasoning as the assistant's OWN message text:
//
//   {"role":"assistant","content":"FOREIGN_REASONING_TEXT",
//    "tool_calls":[…],"reasoning_content":""}
//
// Two consequences, both unacceptable: the new provider reads the previous
// model's private reasoning as if the assistant had said it, and a
// private-class pane's reasoning would ride a Chinese-frontier lane as
// conversation text — silently, which is exactly the data flow the fleet's
// privacy boundary forbids.
//
// Tool pairs are already family-neutral (opaque id + name + arguments + text
// result) and are left untouched. Only reasoning/thinking artifacts are
// family-locked, so those are dropped for every message whose producing family
// differs from the target's, and a message that would be emptied by the drop
// gets a one-line boundary marker instead of vanishing.
//
// Pure and idempotent: running it over its own output changes nothing (the
// marker is plain text, not a reasoning block) and reports no drops.

// Presence marker for the harness: the extension imports this module from the
// same pin it installs the client from, and treats a successful import as
// "request-time normalization is live", which is what makes an in-place
// cross-family switch safe (spec 2435). Bump it whenever the contract changes.
export const NORMALIZER_VERSION = "spec-2435-1";

// Mirror of modules/home/lib/pi-delegate-routing.mjs::providerFamily. Kept
// literal rather than imported because that module belongs to the harness
// (NixOS) and this client must stay loadable without it; the two are pinned to
// the same classification and a divergence is a bug in whichever moved.
export const providerFamily = (provider) => {
  const name = String(provider ?? "");
  if (name === "claude" || name === "claude-bridge") return "anthropic";
  if (name === "codex" || name === "openai-codex" || name.startsWith("codex-")) return "openai";
  if (name === "deepseek" || name === "deepinfra" || name === "zai") return "chinese-frontier";
  if (name.startsWith("mimo") || name.startsWith("xiaomi")) return "chinese-frontier";
  if (name === "cursor") return "cursor";
  return null;
};

// Providers whose requests this extension wraps with the normalizer. The
// harness imports THIS list instead of keeping its own copy, so coverage and
// registration cannot drift apart: an in-place switch is only allowed toward a
// provider that is actually normalized (spec 2435). A listed provider that the
// host does not have is harmless: a switch is only ever attempted toward a
// model the registry already resolved, so it cannot name an absent provider.
//
// deepseek is included: its catalog is Pi-provided (builtin), and a measured
// fixture proved that registering it with just {api, streamSimple} keeps all
// three builtin models and its baseUrl intact.
export const NORMALIZED_PROVIDERS = Object.freeze(["zai", "mimo-desktop", "deepseek"]);

export const normalizesProvider = (provider) =>
  NORMALIZED_PROVIDERS.includes(String(provider ?? ""));

const REASONING_BLOCK_TYPES = new Set(["thinking", "reasoning"]);

export const boundaryMarker = (family) => `[context normalized from ${family}]`;

/** Families that authored the messages of a context, unknown providers aside. */
export const contextFamilies = (messages) => {
  const families = new Set();
  for (const message of messages ?? []) {
    const family = providerFamily(message?.provider);
    if (family) families.add(family);
  }
  return [...families];
};

/**
 * Drop foreign-family reasoning artifacts from a context bound for
 * `targetProvider`.
 *
 * An untagged message (no `provider`) is not treated as foreign: absence of a
 * tag is not evidence of another family, and dropping on a guess would eat the
 * current model's own thinking.
 *
 * @returns {{messages: unknown[], changed: boolean, droppedBlocks: number, markers: number, families: string[]}}
 */
export function normalizeContextForProvider(messages, targetProvider) {
  const targetFamily = providerFamily(targetProvider);
  if (!Array.isArray(messages) || !targetFamily) {
    return { messages, changed: false, droppedBlocks: 0, markers: 0, families: [] };
  }
  const families = contextFamilies(messages);
  let droppedBlocks = 0;
  let markers = 0;
  const out = messages.map((message) => {
    const family = providerFamily(message?.provider);
    if (!family || family === targetFamily) return message;
    if (!Array.isArray(message.content)) return message;
    const kept = [];
    let dropped = 0;
    for (const block of message.content) {
      if (REASONING_BLOCK_TYPES.has(block?.type)) {
        dropped += 1;
        continue;
      }
      kept.push(block);
    }
    // Nothing family-locked here: return the SAME object so an untouched
    // context stays byte-identical and the caller's `changed` stays false.
    if (dropped === 0) return message;
    droppedBlocks += dropped;
    if (kept.length === 0 && message.role === "assistant") {
      markers += 1;
      return { ...message, content: [{ type: "text", text: boundaryMarker(family) }] };
    }
    return { ...message, content: kept };
  });
  const changed = droppedBlocks > 0 || markers > 0;
  return { messages: changed ? out : messages, changed, droppedBlocks, markers, families };
}
