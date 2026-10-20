# Spec 334 — Patch proposal (SHOW ONLY — nothing here is applied)

Companion to `spec.md`. Every hunk is a **proposal**. No file in this repo or in
`~/NixOS` is modified by spec 334; the diffs below exist so a reviewer can land
the chosen one through the normal NixOS/pi review flow. Do not commit from this
spec (shared GitButler checkout, proposal only).

Background and receipts: `spec.md` §2–§5. Summary: the free-gate fail-fast 503 is
byte-identical to the Z.AI queue-timeout marker, but the terminalization predicate
is gated to `provider === "zai"` / `model ∈ {glm-5.3-flash, glm-5.3}`, so the
opencode free models (`opencode-go` / `opencode`, go gate :8770, zen gate) are not
caught and burn-loop on pi's bare-`"503"` string match.

---

## Option A1 — widen the allowlist to the throttle-proxy gates (most surgical)

File: `~/NixOS/modules/home/lib/pi-delegate-routing.mjs`
Function: `planZaiQueueTimeoutTerminalization` (~line 1311)

Keeps the "allowlist, not a bare-`503` substring" design philosophy; simply adds
the two opencode providers that ride the same throttle-proxy fair queue.

```diff
 export const planZaiQueueTimeoutTerminalization = ({message} = {}) => {
   const decline = (reason) => Object.freeze({terminalize: false, reason});
 
   if (message?.role !== "assistant") return decline("not an assistant message");
   if (message?.stopReason !== "error") return decline("not a terminal error");
-  if (message?.provider !== "zai") return decline("not the zai provider");
-  // Both Z.AI Coding Plan models ride the same fair queue, so the same 503
-  // marker is terminal for either. Pedro 14/09/2026 un-retired glm-5.3.
-  if (message?.model !== "glm-5.3-flash" && message?.model !== "glm-5.3")
-    return decline("not a Z.AI Coding Plan model");
+  // Providers that ride the anthropic-throttle-proxy fair queue and emit this
+  // exact queue-timeout marker: the Z.AI Coding Plan lane, plus the opencode
+  // free catalog on the go gate (:8770, provider `opencode-go`) and the zen
+  // gate (provider `opencode`). Spec 334: the free models were not caught and
+  // burn-looped on pi's bare `"503"` match (incident 19/10 ~18:57–19:06).
+  const QUEUE_TIMEOUT_PROVIDERS = new Set(["zai", "opencode-go", "opencode"]);
+  if (!QUEUE_TIMEOUT_PROVIDERS.has(message?.provider))
+    return decline("not a throttle-proxy provider");
```

Notes:
- The exact-marker, zero-content and zero-usage guards below are **unchanged** — a
  non-marker upstream 503 still declines (retries normally).
- Optionally rename `reason: "zai queue admission timeout"` →
  `"throttle-proxy queue admission timeout"` and the `appendEntry` custom type in
  `runZaiQueueTimeoutTerminalization` for accuracy (cosmetic; not required).

### A1 test to add (pure replay, mirrors the existing suite)

```js
// planZaiQueueTimeoutTerminalization terminalizes a free-gate queue-timeout 503.
assert.equal(
  planZaiQueueTimeoutTerminalization({
    message: {
      role: "assistant",
      stopReason: "error",
      provider: "opencode-go",
      model: "longcat-2.5-preview-free",
      errorMessage: "503 proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue\n",
      usage: { input: 0, output: 0 },
      content: [],
    },
  }).terminalize,
  true,
);
// …and still declines a non-marker 503 on the same provider.
assert.equal(
  planZaiQueueTimeoutTerminalization({
    message: { role: "assistant", stopReason: "error", provider: "opencode-go",
               model: "longcat-2.5-preview-free",
               errorMessage: "503 upstream connect/reset before headers",
               usage: { input: 0, output: 0 }, content: [] },
  }).terminalize,
  false,
);
```

---

## Option A2 — marker-agnostic (drop the provider/model gate entirely)

Alternative A1 hunk. The marker string is emitted by the *same*
`_queue_wait_timeout_response` for every gate, so the exact-text + zero-usage
guards alone are sufficient; the provider/model gate exists only as belt-and-
suspenders. If the fleet prefers "never match on provider", use this instead:

```diff
-  if (message?.provider !== "zai") return decline("not the zai provider");
-  if (message?.model !== "glm-5.3-flash" && message?.model !== "glm-5.3")
-    return decline("not a Z.AI Coding Plan model");
+  // Provider-agnostic: the exact queue-timeout marker (emitted only by the
+  // throttle-proxy fail-fast path) is the discriminator; a different 503 from
+  // a real upstream will not carry this body. Spec 334.
```

Trade-off vs A1: broader reach (any provider that somehow emits the identical
body), slightly less surgical. A1 is the conservative default; A2 is offered for
completeness.

---

## Option B — durable upstream pi patch (record only, different repo, out of scope here)

Filed so the fleet does not re-derive it. Repo: `~/Documents/Code/pi-upstream`
(fork of earendil-works/pi). **Not applied, not merged by this spec.**

1. Honor `retry-after` at the agent layer *only when small* —
   `packages/ai/src/utils/retry.ts` + `packages/coding-agent/src/core/agent-session.ts`:
   thread the failed response's `retry-after` (seconds) into the retry wait so
   `delay = max(exponential, min(retry_after_s*1000, ceilingMs))`, and when
   `retry_after_s*1000 > ceilingMs` **do not sleep** — fail terminal with a clear
   message (spec.md §4 Option C). Ceiling ≈ 30 s keeps genuine quick transients
   fast and refuses to stall on a 210–355 s refusal.
2. Honor an explicit "do-not-retry" refusal marker —
   `packages/ai/src/utils/retry.ts`: add a non-retryable pattern for the proxy's
   queue-full refusal (surfaced via the `x-anthropic-throttle-queue-timeout`
   semantics in `errorMessage`) so the bare `"503"` token stops overriding the
   server's explicit "retrying will re-enter the fair queue".

Both are upstream-pi changes with their own review; explicitly out of scope for
this no-rollout proposal. The immediate gap is closed by Option A1/A2 above.

---

## What NOT to change

- Do not set `retry.provider.maxRetries>0` / `maxRetryDelayMs=0` globally (re-enters
  the wall, broadens large-`retry-after` sleeps to every provider).
- Do not set `retry.maxRetries=0` fleet-wide (kills legit fast transient retries).
- Do not edit proxy `src/` for a client behavior (other slice).
