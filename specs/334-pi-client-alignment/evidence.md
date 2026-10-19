# Spec 334 — Evidence (raw receipts)

All commands run on desktop, 19/10/2026. Paths are live on this host.

## E1 — Proxy fail-fast 503 shape (header + body)

`src/anthropic_throttle_proxy/proxy.py` `_queue_wait_timeout_response` (lines 819–830):

```python
    return web.Response(
        status=503,
        headers={
            "retry-after": str(retry_after_s),
            config.QUEUE_TIMEOUT_HEADER: "1",
        },
        text=(
            "proxy queue wait exceeded; slots saturated — retrying will re-enter the fair queue\n"
        ),
    )
```

`src/anthropic_throttle_proxy/config.py:542`:
```python
QUEUE_TIMEOUT_HEADER = "x-anthropic-throttle-queue-timeout"
```

The marker body is byte-identical to the extension's `ZAI_QUEUE_TIMEOUT_MESSAGE`
(`~/NixOS/modules/home/lib/pi-delegate-routing.mjs:1261`).

## E2 — pi agent-level retry ignores `retry-after`

`pi-upstream/packages/ai/src/utils/retry.ts`:
- line 42: `"503",` (in `RETRYABLE_PROVIDER_ERROR_PATTERN`) → our 503 is retryable
- line 37: `"rate.?limit",`, line 40: `"500",`
- line 128: `export function retryDelayMs(...)` = `min(baseDelayMs * 2^(attempt-1), maxAgentDelayMs ?? 60_000)` — no server-delay input
- line 126: `DEFAULT_MAX_AGENT_RETRY_DELAY_MS = 60_000`

`pi-upstream/packages/coding-agent/src/core/agent-session.ts`:
- line 3709: `_isRetryableError` → `isRetryableAssistantError`
- line 3762: `_prepareRetry`
- line 3776: `const delayMs = retryDelayMs(settings, this._retryAttempt);`

## E3 — pi provider-layer honors the header but is off by default + capped

`pi-upstream/packages/ai/src/utils/provider-retry.ts`:
- line 53 `getRetryDelayMs`: reads `retry-after-ms` / `retry-after` **headers** (54–64)
- line 39 `validateServerRetryDelayMs` throws when delay > `maxRetryDelayMs`
- line 1: `DEFAULT_MAX_RETRY_DELAY_MS = 60_000` → a 210 s `retry-after` becomes an error, not a wait
- line 111: `const maxRetries = options.maxRetries ?? 0;` → layer disabled unless configured

## E4 — live pi retry config (no baseDelayMs, no provider.*)

`~/.pi/agent/settings.json`:
```json
"retry": { "enabled": true, "maxRetries": 4 }
```
`httpIdleTimeoutMs` unset → docs default 300000.
No `retry.baseDelayMs`, `retry.maxAgentDelayMs`, or `retry.provider.*` anywhere in
`~/NixOS/modules/home/{pi.nix,pi-options.nix}` (grep: no hits).
→ Agent delays = 2 s, 4 s, 8 s, 16 s (4 retries), server `retry-after` discarded.

## E5 — free models ride `opencode-go` (go gate :8770), not `zai`

`~/.pi/agent/models.json` → `providers["opencode-go"].models` (29-entry list),
each overriding `baseUrl` to `http://127.0.0.1:8770/openai/v1`. Free-catalog ids:
`beam-free`, `longcat-2.5-preview-free`, `mimo-v2.6-flash`, `glm-5.3`,
`deepseek-v4.1-flash`, `space-bunny`, `glm-5.3-flash`, …
Provider key in pi = `opencode-go` (go gate); zen gate is the sibling `opencode`.

## E6 — terminalization is zai-gated (the gap)

`~/NixOS/modules/home/lib/pi-delegate-routing.mjs` `planZaiQueueTimeoutTerminalization`:
```js
if (message?.provider !== "zai") return decline("not the zai provider");
if (message?.model !== "glm-5.3-flash" && message?.model !== "glm-5.3")
  return decline("not a Z.AI Coding Plan model");
…
if (text !== ZAI_QUEUE_TIMEOUT_MESSAGE) return decline("not the queue-timeout marker");
```
`~/NixOS/modules/home/lib/pi-parity-extension.nix` (~line 1858) wires this into a
`message_end` handler that rewrites `errorMessage` to a non-retryable terminal
string; the in-file comment (lines 1794–1815) documents that the rewrite decides
the `_isRetryableError` → `_prepareRetry` classification. So a matching free-gate
message would already be stopped — it just never matches because of the provider gate.

## E7 — retry ordering seam (extension rewrite decides retry)

`~/NixOS/modules/home/lib/pi-parity-extension.nix` (lines 1794–1815 comment):
`_handleAgentEvent` awaits the extension emit; `emitMessageEnd`'s replacement goes
through `_replaceMessageInPlace`; only then does `this._lastAssistantMessage =
event.message` run — the object `_handlePostAgentRun` hands to `_isRetryableError`.
Rewriting `errorMessage` in `message_end` therefore controls the retry decision on
the shipped code path.
