# 240 — independent gate: findings and disposition

Gate: full **GLM-5.3** on the exact head `4660814` (generator family OpenAI GPT-6 Astra,
so the review rides a different family). Verdict: **REQUEST CHANGES — one BLOCKER**,
four MAJOR, several MINOR. Every finding is answered below; the fixes land on top of
`4660814` and every claim is backed by the evidence files named here.

## BLOCKER 1 — the cut could separate a tool call from its answer — FIXED

`fit_chat_completions_body` minimized the dropped count with no knowledge of pairing.
On the native OpenAI shape (`assistant.tool_calls` turn + the `role:"tool"` turn that
answers it), a cut landing mid-pair leaves a tool answer whose call is gone: an invalid
transcript, i.e. the proxy would be the *cause* of the very refusal it exists to prevent.
The gate also named the blind spot that hid it: no test used a native `tool_calls` body.

**Independently reproduced before fixing** (`evidence/orphan-blocker-both-heads.txt`),
one script against both heads, same fixture:

| head | trimmed transcripts | with an orphaned tool answer |
|---|---|---|
| `4660814` (before) | 30/30 | **16** — e.g. prefix `[system, system, tool, assistant]`, orphan id `c27` |
| after the fix | 30/30 | 0 |

The test's own assertion, driven against the old implementation (only the new meta-tuple
unpacking adapted), fails **16/30** — `evidence/orphan-assertion-on-old-head.txt`.

Both legs come from one short script, kept out of the repo on purpose: it has to reuse
the test's fixture verbatim (deterministic aperiodic sizes, `40 + (i*i*37 + seed*97) % 1300`
for the call and `40 + (i*53 + seed*211) % 1300` for the answer) and the `orphans()` check,
and the repo's own slop gate correctly refuses that duplicated code. Reproduce instead:
checkout `4660814`, copy `tool_transcript`/`orphans` out of `tests/test_routing_chat_budget.py`,
run the loop for `seed in range(30)` and count the outputs where `orphans(kept)` is
non-empty. The expected numbers are the two rows above.

**Fix:** `_answers_a_dropped_call` + snap the cut forward past any leading tool answer;
when every remaining cut would keep an orphan (or would eat the protected tail) the
original body is returned with `reason="unfittable"`. Covers both shapes: native
`role:"tool"`/`tool_call_id`, and a `tool_result` content block (reachable when the
normalizer passes a malformed body through untouched).

**Falsifiers added:** `test_cut_never_orphans_a_tool_answer` (30 deterministic uneven
transcripts, judged by an `orphans()` helper that reads only roles and ids — independent
of the implementation's own predicate), `test_cut_never_orphans_an_anthropic_tool_result_block`,
`test_unfittable_when_every_cut_would_orphan_a_tool_answer`.

## MAJOR 2 — every probe re-serialized the whole body — FIXED

The binary search dumped the entire body per step: ~10 full dumps for a 3 MB body, and
tens of seconds for the 128 MiB `client_max_size` admits — on the event loop of a
single-process fleet proxy.

**Fix:** size each turn once (one small `json.dumps` per turn), suffix-sum them, and
evaluate the size arithmetically in O(1) per probe; the emitted body is still produced by
a real dump and verified against the budget before it replaces the client's bytes.
**Falsifier:** `test_sizing_serializes_roughly_once_per_turn` counts serialized bytes
through a wrapped `json.dumps` and requires < 3× the body (the old shape was ~11×).

## MAJOR 3 — a give-up was invisible — FIXED

`reason="unfittable"` returned in silence, so the counter could read green while the 413
kept happening — the exact confusion this change exists to remove.

**Fix:** the function now returns `(body, meta)` (the shape `body_shrink.shrink_body`
already uses), `M_CHAT_BODY_UNFITTABLE` counts the give-up, and `chat_body_unfittable` is
logged. The log no longer conflates the two transforms' sizes
(`normalized=`/`final=`/`turns_dropped=`).

## MAJOR 4 — bulk-in-tail bodies stay unfittable — ACCEPTED, now visible

Dropping whole turns cannot help when the live tail itself is over budget. That is
inherent to the lever, is the documented "refuse as before" branch, and is now countable
and greppable (MAJOR 3). The alternative lever (stubbing the tail's tool results) means
editing the live turn, which is the behaviour this design refuses.

## MAJOR 5 — cache economics and the guessed band — ACCEPTED AS A TRADEOFF, telemetry added

The gate is right that (a) a session past the budget is re-trimmed on every turn with a
shifting prefix, so its cache hit rate goes to zero for the session's life, and (b) the
`[budget, real-gate)` band degrades requests that previously succeeded whole. Both are
consequences of choosing *working-but-clipped* over *broken*, which is the deliberate
trade the change makes, and both are documented in the README row.

Its constructive half is adopted: the fit logs `normalized=`, `final=` and
`turns_dropped=`, and the give-up is counted, so the real ceiling can be read out of
fleet data instead of guessed. The breadcrumb was also reworded — the old text invited
re-reading files ("re-read it if you need something that is missing"), which would
re-bloat the very request it just fitted; it now asks for the specific file or span.

## MINOR 6 — `endpoint.port` could raise where the old inline check swallowed it — FIXED

`.port` raises `ValueError` on a non-numeric or out-of-range port, and extracting the
predicate moved that access outside the old broad `try`. **Fix:** the whole predicate body
is inside the guard. **Falsifier:** `test_a_malformed_port_is_not_an_endpoint_and_never_raises`.

## MINOR 7 — a compact re-dump that fits is discarded — DECLINED, with reason

When the client's bytes are over budget only because of their own formatting, the body is
returned untouched (`reason="fits-when-compact"` = MAJOR 5's `low == 0` branch). Dropping
history to win an argument about whitespace is worse than not dropping it, and
re-serializing a body without dropping anything rewrites the client's bytes for no
behavioural gain. Pinned by
`test_a_pretty_printed_body_is_not_trimmed_to_win_an_argument_about_whitespace`.

## MINOR 8 — the guard skipped a droppable turn — FIXED

The old `len(messages) <= CHAT_KEEP_TAIL + 1` guard ignored whether an anchor existed, so
a body with exactly `KEEP_TAIL + 1` live turns was passed through although one turn was
droppable. The guard now tests the live turns. **Falsifier:**
`test_a_body_with_only_just_enough_turns_still_trims`.

## MINOR 9 — trailing-dot / percent-encoded hosts are false negatives — DECLINED, with reason

`target` is built from our own `THROTTLE_UPSTREAM` config plus the client's path, so a
trailing-dot or percent-encoded host would have to be authored into our config; the
normalizer beside it is equally strict, and consistency between the two transforms is the
property that matters more than covering a spelling we never generate.

## MINOR 10 — breadcrumb placement, env parsing, KEEP_TAIL floor, counter labels — DECLINED

- Folding the breadcrumb into the anchor would mutate the client's own system prompt;
  inserting a separate leading system turn keeps the assignment byte-identical.
- `THROTTLE_CHAT_MAX_BODY_BYTES=abc` failing at import matches every other knob in this
  repo (`CODE_MAX_TOKENS`, `PRIORITY_MAX_BODY_BYTES`, …); a lone special case would be the
  inconsistency.
- `max(2, ...)` matches `body_shrink.KEEP_TURNS`'s existing floor.
- A `model` label on the new counter was declined: the value comes from the client's body,
  so it is unbounded cardinality — the repo already warns about exactly this for the
  code-role metric.

## Not addressed because it is out of scope here

Live `api.z.ai` acceptance of a fitted transcript. It needs a Nix pin bump and a service
restart, it is gated, and no synthetic test substitutes for it.
