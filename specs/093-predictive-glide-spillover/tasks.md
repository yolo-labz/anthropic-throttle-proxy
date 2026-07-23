# Spec 093 — tasks (v2: unified :8760 ingress)

One slice per row, smallest correct diff, each lands with its acceptance test
green + full suite + ruff. Throttle-path → the speckit-make cross-family
adversarial panel (codex + Opus) must ALLOW each slice before merge. Execute
on/after 28/07 (kimi-k3 GA + codex recovery). The per-lane throttles
(:8765/:8766/:8767) are prerequisites already live/landed; this builds ONLY the
router in front of them.

- [ ] **S1 — ingress skeleton + no-op-when-unset.** A new Anthropic-shape
      aiohttp server on `:8760` that forwards a request to a configured
      default lane (`:8765`) path-preservingly, identical to pointing at the
      lane directly. With the ingress disabled, claude-code points at `:8765`
      as today (invariant 5). Acceptance: ingress passes through to `:8765`
      byte-identical; a probe `:8760/v1/messages` → same as `:8765`.
- [ ] **S2 — role inference from the request model (no new header).** Parse the
      request body model → tier: `opus`/`fable`→generate, `sonnet-5`→judge,
      `sonnet-4-6`/`haiku`-slot→bulk/subagent. Acceptance tests per tier; the
      subagent slot (`claude-sonnet-4-6`, PR #1183) routes to bulk.
- [ ] **S3 — gauge-driven lane selection.** Read each lane's retry-after / 5h/7d
      window state (the proxies already expose `/__throttle/health` + the
      Anthropic/z.ai unified gauges): for the request's role, walk the role's
      chain (generate: Anthropic→Kimi→GLM→HOLD; bulk: Kimi/GLM first) and pick
      the first lane whose account is open + under threshold. Acceptance:
      simulate an account lock → next request auto-advances (invariant 3);
      bulk never selects Anthropic (invariant 2).
- [ ] **S4 — model-remap on egress + session stickiness.** Remap `claude-*` →
      the chosen lane's id on the forward body; keep the client-facing id. Pin
      a session to its lane unless it hard-locks (cache economics — spill at
      session boundaries, not per-request). Acceptance: egress body model ==
      mapped id, client id unchanged (invariant 4); a session stays on its
      lane across requests until the lane locks.
- [ ] **S5 — never-hard-fail + don't-silently-downgrade guards.** If any lane
      can serve the role, serve it (invariant 1); fail only when ALL lanes for
      the role are capped. Before 27/07 (no kimi-k3), a generate-role demand
      under full Anthropic cap HOLDS + flags, not a silent kimi-k2.6/GLM 200
      (invariant 6). Acceptance: all-Anthropic-capped + bulk role → served via
      Kimi/GLM; all-capped + generate + pre-kimi-k3 → HOLD/flag.
- [ ] **S6 — observability.** Counters per (role→lane) decision; a Kimi
      **low-balance gauge** (poll the Moonshot balance, alert <$5); surface
      GLM/Anthropic window gauges at `:8760` so spillover + fleet state are
      visible in one place (invariant 7). Acceptance: a `/metrics` scrape shows
      the per-lane decision counts + the Kimi balance.
- [ ] **S7 — Nix wire-through.** A `unified-throttle-ingress` HM module
      (`:8760`) pointing at the three lanes; flip the fleet's claude-code
      `ANTHROPIC_BASE_URL` to `:8760`. Per-lane proxies stay individually
      reachable as the SPOF fallback. Acceptance: desktop config evaluates;
      a claude-code tab against `:8760` routes per the chain under a simulated
      Anthropic cap.

## Verification bar (the done-state, externalized)
- `uv run pytest` green (incl. the new invariant tests S1–S6); `ruff` clean.
- speckit-make cross-family panel (codex reward-hacking + Opus correctness +
  codex security) all ALLOW each slice.
- A live or simulated run: with 2 of 3 Anthropic accounts 7d-capped, the
  fleet still serves every role (bulk on Kimi/GLM, generate on the open
  account, overflow generate to Kimi-k3 post-27/07) with zero hard-fails +
  zero `rejected` trips on the open Anthropic account (invariants 1, 2, 6).
