# Tasks

- [x] T001 Recover the dropped commit's intent from the closed PR #236 branch and re-verify it against the post-#237 tree.
- [x] T002 Land the trim in `routing.py`, sharing ONE endpoint predicate with the normalizer so the two transforms cannot disagree.
- [x] T003 Hook it at the per-attempt egress boundary beside the normalizer; drop the dead ingress call site #237 already removed.
- [x] T004 Make the drop search bounded (binary search, not one re-serialization per dropped turn on the event loop).
- [x] T005 Verify the hook is REACHABLE: proxy app runs `client_max_size=128 MiB`, so an oversize body is not refused before the trim runs.
- [x] T006 Unit falsifiers + real forwarding-path regressions (incl. central-keeps-original and retry coverage).
- [x] T007 RED on exact original head `dfa297c` for the behavioral reason; GREEN after; full pytest + Ruff.
- [x] T008 Observability: counter + log line so a clipped history is countable, not silent.
- [ ] T009 Coordinator: exact-head different-family review, CI disposition and landing.
- [ ] T010 Separately authorized deployment and live cross-family replay (needs a Nix pin bump + service restart).

## Considered and rejected: reusing `body_shrink.py`

`shrink_body` already trims oversize `/v1/messages` bodies (28 MiB cap, tool_result
and image block stubs, with counters). Reuse was rejected on evidence, not
convenience:

- Its lever cannot reach the failure. `_iter_trimmable_blocks` skips any message
  whose `content` is not a list, so a string-content (OpenAI-shaped) body has
  nothing trimmable and would still 413. The measured victim was a 707k-token
  seat, and this lane receives both shapes.
- Its cap is a single global number chosen for Anthropic's 32 MB limit; a ~1.8 MB
  lane budget would need a second, per-path cap and would change the semantics of
  the knob it already owns.
- Its seam is the handler, once per request, before target selection — so central
  would receive already-trimmed bytes. #237 pinned the opposite contract for this
  endpoint (central gets the client's original bytes; the DIRECT attempt is shaped),
  and the per-attempt seam covers retries for free.
