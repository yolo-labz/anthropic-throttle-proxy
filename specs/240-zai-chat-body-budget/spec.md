# 240 — Z.AI chat-completions request budget

Date: 22/09/2026. Re-lands the budget trim proposed in PR #236's fifth commit
(`6860c1d`), which was dropped when #236 was closed as superseded: #237 imported
"the exact four commits" of that branch, so the last one was never landed.
No deployment in this slice.

## Required behavior

- Fit an oversize body to this lane's request budget by dropping the OLDEST turns, and only for the exact Z.AI coding endpoint (`https://api.z.ai` + `/api/coding/paas/v4/chat/completions`). Every other host, scheme, port and protocol path keeps byte-identical input.
- The system prompt is an ANCHOR and is never dropped. The last `THROTTLE_CHAT_KEEP_TAIL` turns are kept intact — the model must still see the live turn.
- The cut never lands between an assistant tool call and the answer to it: an orphaned `role:"tool"` turn (or tool_result block) is an invalid transcript, which would make the proxy itself the cause of a refusal. When every remaining cut would orphan one, the original body is forwarded.
- A breadcrumb turn records how many turns were removed and asks for the specific file or span needed, so the model knows its history was clipped instead of inventing the missing part — and is not invited to re-read everything and re-bloat the next request.
- What the proxy rewrites must FIT the budget it rewrote it for. When even the protected tail is over budget, the ORIGINAL body is forwarded: the budget is a guess at an unpublished ceiling, and a guess is not evidence that this request is too big. A lane that refuses a big body should refuse it, not receive something mangled.
- Sizing is O(one small dump per turn) plus a final dump: a full re-serialization per binary-search probe would stall the event loop (a 3 MB body ≈ 10 full dumps; `client_max_size` admits 128 MiB).
- No new dependency, no vendor SDK, no routing/capacity change. The endpoint predicate is identity only.
- The trim happens at the per-attempt egress boundary (`forwarding._forward_once`), the same seam as the text-only normalizer, so it covers initial, pushback and central-to-direct retries while central keeps receiving the client's original bytes.
- A fit and a give-up are both observable: `anthropic_chat_body_fitted_total` and `anthropic_chat_body_unfittable_total` plus one `chat_body_fit` / `chat_body_unfittable` log line, because clipping a session's history must not be silent and a green fitted counter must not hide the 413 that is still happening.

## Why the default sits under the plausible ceiling

The only measured refusal is at ~3 MB (707k tokens), and the message is not ours
(our app raises aiohttp's 1 MiB default to 128 MiB, and aiohttp's own 413 text
differs). Web evidence puts the message text at Bun's body buffering error
(observed at ~2.1–2.4 MB bodies in another deployment) and axum's
`DefaultBodyLimit` at 2 MB. Fitting to 2 MiB would satisfy our own check and
still be refused upstream — the failure this exists to remove — so the default is
`1_800_000`, env-tunable, documented as a guess rather than a protocol fact.

## Acceptance

Runnable regressions must fail on exact original head `dfa297c` for the real
reason and pass after the change; full pytest, Ruff lint and format must run.
Independent review and any merge belong to the coordinator; live verification
against `api.z.ai` remains unperformed (it needs a Nix pin bump and a service
restart — out of scope here).

## Independent gate

The different-family review of head `4660814` returned one BLOCKER (the cut could
orphan a tool answer), four MAJOR and several MINOR findings. Every one is answered —
with its evidence, and with the reason for each decline — in `review.md`.

## Provenance

Generator: OpenAI GPT-6 Astra/xhigh (this head). Original commit author of the
dropped logic: same lineage (PR #236). Different-family review required before
merge.
