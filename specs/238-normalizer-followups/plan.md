# 238 — normalizer follow-ups (issue #238) — plan + result

Scope (ROLLOUT-INTEGRATION): resolve the three retained synthetic follow-ups of
issue #238 with synthetic regression. Live ZAI replay (#238 item 4) stays
separate while admission is closed. No provider bypass, no full-test storm.

## 1. Tool linkage (pinned failure mode)

A content-level `tool_use` rendered as text orphaned its `role:"tool"` partner.
Resolution: reconstruct NATIVE `tool_calls` from linkable blocks (id+name) on
assistant turns without pre-existing `tool_calls` — the shape this endpoint
accepts (PR #236: 1210 hits content[] types, never tool_calls). When linkage is
impossible (no id/name), `_relink_tool_results` degrades the orphaned partner
to explicit `[tool result]` text instead of a validator-rejected reference.
Covered references and native protocol fields are never touched.

## 2. `_KNOWN_BLOCK_TYPES` drift guard

Set derived from the branch-predicate group frozensets (`_TEXT_KINDS`,
`_THINKING_KINDS`, `_TOOL_CALL_KINDS`, `_TOOL_RESULT_KINDS`, `_IMAGE_KINDS`);
branches consult the groups. Regression scans `_part_as_text`/`_native_tool_call`
source for literal `kind` comparisons outside the derived set.

## 3. Header rebinding contract

`_rebind_headers` extracted with the load-bearing premise documented and
asserted: plain mapping only (what `proxy.handler` builds from a dict
comprehension); a multi-dict would silently collapse duplicate names — now it
fails loudly.

## Acceptance (synthetic only)

- `tests/test_routing_text_only.py` + `tests/test_forwarding_paths.py`: 128 pass
  (6 new: linkage, orphan degradation, covered/native preservation, derived
  set, literal-drift scan, header contract).
- Forwarding-adjacent regression slice: `test_keepalive_hold.py` +
  `test_credential_quarantine.py` 72 pass.
- `ruff check` + `ruff format --check` clean on src/tests.
- Live replay deliberately NOT run (admission closed).
