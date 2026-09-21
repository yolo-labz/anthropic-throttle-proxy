# 237 — Z.AI content normalizer correctness repair

Date: 21/09/2026. Replaces the proposal in PR #236; no deployment in this slice.

## Required behavior

- Flatten recognized internal content blocks only for the Z.AI Coding Plan chat-completions endpoint. Other providers and the Anthropic Messages protocol stay byte-identical.
- Preserve every valid preceding turn when the final thinking-only turn needs a placeholder.
- Preserve native `tool_calls`, `tool_call_id`, roles and tool-result messages; the observed 1210 rejection concerns content-block types, not the native tool protocol.
- Preserve complete tool arguments/results, including empty/falsey values. No silent length truncation.
- Malformed JSON, message containers and nested blocks pass through unchanged, without exceptions or partial normalization. Unknown block schemas also pass through rather than silently losing data.
- Keep deliberate thinking removal and explicit image-omission placeholders for the text-only endpoint. Existing empty strings are not grounds for deleting a turn.
- Exercise the real forwarding path, including retries and central/direct selection, rather than only calling a helper that ingress never reaches.

## Acceptance

Runnable regressions must fail on exact original head `ae9e9e9f96aa01ec0275b6ceae495e9a4c797506` and pass after repair. Full project pytest, Ruff lint and format checks must run. Independent review and any merge belong to the coordinator; live verification remains unperformed.
