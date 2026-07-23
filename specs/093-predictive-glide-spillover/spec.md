# Spec 093 — Unified `:8760` ingress: the "never run out of AI" router

**Status:** design v2 (23/07/2026) — supersedes the v1 in-proxy-spillover
mechanism. Refined to a **single role-tagged ingress** per the joint
routing-policy/proxy architecture (`/tmp/never-run-out-architecture.md`).
Teed up for `speckit-make` execution on/after **28/07/2026** (kimi-k3 GA 27/07
+ codex adversarial-gate recovery 28/07). Prerequisites landed: Kimi overflow
lane (#1326/#1328/**#1329**) + z.ai lane (:8766) + sock-read fix (#130).

## Problem (Pedro's objective)

> "Use ALL of our AI limits through the week but without ever hitting the limit wall."

Today the three lanes (`:8765` Anthropic, `:8766` z.ai, `:8767` Kimi) are
**independent, manually selected** — each tab hardcodes one
`ANTHROPIC_BASE_URL`. When Anthropic 7d-caps (live now: 1 of 3 accounts open,
two locked ~35h/~46h), a claude-code tab sits idle or hammers a locked account;
it cannot see that GLM/Kimi have headroom. The lanes never compose into a
chain. **Unification is the whole game.** And the current `_maybe_glide`
(`proxy.py:518`) is a one-shot concurrency brake — the wrong mechanism (trips
when off per the 30/05 scar, starves when on).

## Goal

A **single ingress** (`:8760`) every claude-code tab points at. It routes each
request on **role + live gauges** across the lanes so there is always a next
lane — the fleet degrades gracefully and **never hard-fails for lack of a
model**. Ride each lane to its cap, spill the excess to the next, hold only
when ALL lanes are capped AND the role demands a quality none can serve.

```
request (role inferred from model) ──► unified ingress :8760
  ├─ GENERATE/driver : Anthropic(open, budget_paced) → Kimi-k3(27/07+) → GLM-5.2 → HOLD(flag)
  ├─ JUDGE/eval      : Anthropic-Sonnet → GLM-5.2 → Kimi-k2.6            (never small tier)
  ├─ BULK/subagent   : Kimi-k2.6 / GLM-5-turbo FIRST — NEVER touches Anthropic
  └─ GATE(adversarial): different-family — codex(28/07+) / GLM / Anthropic-recovered
```

The guarantee: **GLM (flat) is the floor; Kimi (pay-go, elastic) is the
pressure valve; Anthropic is reserved for premium generate.** Bulk rides the
cheap lanes → there is always a serving lane → cannot run out.

## Mechanism (mechanism decisions — answers to the routing-tab Q's)

**Q1 — shape: unified `:8760` ingress (chosen), NOT per-lane + thin router.**
A new Anthropic-shape router in front of the three per-lane throttles. The
per-lane proxies (`:8765/:8766/:8767`) stay as the throttles (per-bearer
semaphore + AIMD + quota holds); `:8760` is purely the **router**: role-tag →
gauge check → pick lane → forward (path-preserving, model-remap on the egress
to the chosen lane). This composes all lanes (the in-proxy spillover of v1
only handled Anthropic→Kimi). SPOF guard: the per-lane proxies stay
individually reachable (`:8766/:8767`) as a manual fallback if `:8760` dies.

**Q2 — role inference: from the request model, no new header.** claude-code
sends the model in the body; the ingress infers role from the tier:
`opus`/`fable` → generate, `sonnet-5` → judge, `sonnet-4-6`/`haiku`-slot →
bulk/subagent. The subagent fan-out is the cleanest lever: it already runs
`claude-sonnet-4-6` (PR #1183) → route that tier to the Kimi/GLM bulk chain so
fan-out (the ~7× token burn) spends the elastic lanes, not Anthropic. (A new
header is optional later for ambiguous cases; model-tier is enough for v1.)

**Q3 — GLM is NOT unbounded: it is a 4th capped lane.** z.ai has 5h/7d quota
(codes 1316/1317) + concurrency caps (1302), handled by the proxy's code-class
split (PR #69). It is currently below cap (`:8766` healthy, `unified={}`, no
retry-after) but it IS a capped lane. So the chain backs **GLM↔Kimi
cross-lane** (the two cheap lanes back each other, not just Anthropic), each
with its own gauge.

**Spill trigger** = the existing per-bearer retry-after + 5h/7d gauges (the
proxy already parses them for Anthropic + z.ai): when a lane's account is
locked OR window > threshold, advance to the next lane automatically — **no
per-tab base-URL edits**.

**Session stickiness** (cache economics): spill at **session boundaries**, not
per-request, when a big cached prefix is in play (an Anthropic mid-session
switch forces a slow uncached turn; the 90% read discount is exact +
model-specific). Keep the boot prefix byte-stable. Within a session, hold the
lane unless it hard-locks.

## Hard constraints / invariants (each → an acceptance test)

1. **Never hard-fail while a lane can serve.** If any lane can serve the role,
   the ingress uses it; a request fails only when ALL lanes for that role are
   capped. (Test: Anthropic all-capped + Kimi healthy → bulk + judge still
   served via Kimi/GLM.)
2. **Bulk never touches Anthropic.** A bulk/subagent-tier request routes to
   Kimi/GLM even when Anthropic is wide open. (Test: bulk model-tier + Anthropic
   idle → assert Kimi/GLM path, zero Anthropic.)
3. **Gauge-driven, not per-tab config.** Spill on live retry-after / window
   gauges; a tab never needs a base-URL edit. (Test: simulate an account lock
   mid-stream → next request auto-advances lane.)
4. **Model-remap on egress only.** `claude-*` → the chosen lane's id (`kimi-k3`,
   `glm-5.2`) on the forward; the client keeps its `claude-*` id. (Test: egress
   body model == mapped; client-facing id unchanged.)
5. **No-op when ingress unset.** With the ingress disabled, claude-code points
   at `:8765` directly as today (zero behavior change).
6. **Don't silently serve a weaker model as Opus.** Before 27/07 (no kimi-k3),
   a premium-generate demand under full Anthropic cap must HOLD + flag, not
   silently serve kimi-k2.6/GLM as if it were Opus. (Test: all-Anthropic-capped
   + generate role + pre-kimi-k3 → HOLD/flag, not a k2.6 200.)
7. **Observability.** Counters per (role → lane) decision + a Kimi **low-balance
   gauge** (alert < $5) + GLM/Anthropic window gauges, so spillover is
   data-driven and the fleet state is visible.

## Honest limit

"Exactly 100% Anthropic utilization + zero trips" is unattainable (Anthropic's
budget is opaque + the `allowed_warning`→`rejected` boundary is noisy). The
verifiable bar: **zero hard-fails while a lane can serve** (invariant 1) +
**≥0.95 binding utilization at a near-zero `rejected` trip rate** on Anthropic,
the residual served by Kimi/GLM. The point is graceful degradation + never
running out, not a perfect glide.

## Out of scope (explicit)

- Per-request predictive scoring (FrugalGPT/RouteLLM cascades) — the
  role+gauge chain is enough for a 3-lane fleet.
- Lanes beyond Kimi/GLM (DeepSeek/Gemini) — add later via the same router.
- Changing the client-facing model id.
