# Spec 093 — Predictive glide spillover: use the full weekly budget, never trip the wall

**Status:** design (23/07/2026). Teed up for `speckit-make` execution on/after
**28/07/2026** — that date aligns the two prerequisites this build needs live:
the **`kimi-k3` model GA** (27/07, the spillover target model) and the **codex
adversarial gate** recovery (28/07, the mandated cross-family review for a
throttle-path change). The Kimi overflow lane (NixOS #1326/#1328/#1329) +
proxy sock-read fix (#130) are the landed prerequisites.

## Problem (Pedro's objective, live-evidenced 23/07)

> "Use ALL of our AI limits through the week but without ever hitting the limit wall."

The fleet's binding constraint is the **7-day Anthropic ceiling per Max sub**
(all three accounts 7d-cap concurrently under load; ccusage July ≈ $27k
API-equiv burn — cost is 45–90× leveraged on the flat subs, so capacity — not
dollars — is what bites). Two failure modes today, neither meets the objective:

1. **Trip the wall.** `THROTTLE_UTILIZATION_TARGET` default-off → accounts ride
   to utilization 1.0 → `rejected` → multi-day lockout + the 07/07
   phantom-storm user-facing pain. Wastes the lockout AND hits the wall.
2. **Starve.** `TARGET` on → `_maybe_glide` shrinks concurrency **once per
   reset window** (`proxy.py:518`) → collapses throughput (the 30/05 scar that
   keeps it default-off) → leaves the top ~10% unused.

The current glide is the wrong mechanism: shrinking concurrency doesn't
proportionally slow budget burn (slots refill over time) and it starves the
fleet. **Neither uses-all-without-tripping.**

## Goal

Ride each Anthropic account **into** the `allowed_warning` band (the 90–100%
zone Anthropic still serves but flags) and **spill the excess demand to the
Kimi overflow lane** (`:8767`, model-remapped) instead of shrinking
concurrency or queueing into a trip. Anthropic holds at sustainable capacity
just under `rejected`; Kimi absorbs whatever would cross the line. Net:

- **Near-full Anthropic utilization** (ride `allowed_warning` → ~95–99%).
- **All demand served** (Anthropic + Kimi aggregate throughput maintained).
- **Zero hard-wall trips** (spill BEFORE `allowed_warning` → `rejected`).

## Architecture decision — keep it in the proxy (NOT a separate gateway)

**Proxy-to-`:8767` predictive spillover with model-remap**, in the `:8765`
binary. Rejected the alternative (a `ccr`/LiteLLM gateway in front of both
proxies) because: the `:8765` proxy **already holds the `allowed_warning`
signal** (it parses `anthropic-ratelimit-unified-*` every response); a gateway
would have to re-fetch that signal from `:8765` health, adding latency + a
coupling the in-proxy design avoids. The Nix-research "routing ≠ throttling,
they compose" holds for *generic* routing; for *this* predictive-spill-on-
Anthropic-utilization case the proxy is the right home because it IS the
component with the signal.

The only "bend" to the transparent proxy is an explicit, gated, logged
spillover mode — narrower than a general translation layer.

## Mechanism

New config (all default-off / no-op when unset):

- `THROTTLE_SPILLOVER_UPSTREAM` — the overflow lane origin
  (e.g. `http://127.0.0.1:8767`). Empty = feature off (zero behavior change).
- `THROTTLE_SPILLOVER_MODEL_MAP` — `claude-opus-4-8:kimi-k3,claude-sonnet-5:kimi-k3,...`
  applied ONLY on spillover (the client's `claude-*` id → the Kimi id).
- `THROTTLE_SPILLOVER_GLIDE_THRESHOLD` — the binding-utilization floor at which
  an account is "glide-saturated" (default = the `allowed_warning` signal
  itself; configurable for tuning).

Decision rule (per incoming `POST /v1/messages`, after the existing
budget-paced account selection):

1. If a non-saturated Anthropic account is routable → use it (unchanged).
2. Else if **all** accounts are glide-saturated (`allowed_warning`+, or binding
   util ≥ threshold) AND spillover is configured → forward to the spillover
   upstream with the model remapped (path-preserving; the `:8767` proxy stamps
   the Moonshot Bearer via its own `apiKeyRouting`).
3. Else (spillover unset or the spillover lane is down) → fall back to the
   existing least-bad Anthropic queue (do NOT fail the request).

Spillover-lane health is read from the existing `fleetHealthUrls`/central-style
probe (a down `:8767` ⇒ rule 3, never a hard fail).

## Hard constraints / invariants (each maps to an acceptance test)

1. **Never trip `rejected`.** Spill BEFORE `allowed_warning` → `rejected`. A
   fleet with all accounts at `allowed_warning` must route new requests to
   spillover; none may reach an Anthropic account that then returns
   `rejected`. (Test: simulate all accounts `allowed_warning`; assert spillover
   path; assert zero Anthropic `rejected`.)
2. **Maximize utilization (don't spill early).** Accounts below the glide
   threshold must route to Anthropic, not spillover. (Test: accounts at 0.80
   with `THROTTLE_SPILLOVER_GLIDE_THRESHOLD` default → Anthropic path.)
3. **Model-remap correctness + only-on-spillover.** The map applies to the
   spillover forward only; Anthropic-bound requests keep their original
   `claude-*` id. (Test: spillover forward body model == mapped; Anthropic
   forward body model == original.)
4. **No-op when unset.** `THROTTLE_SPILLOVER_UPSTREAM=""` ⇒ behavior
   byte-identical to today (all existing tests pass unchanged).
5. **Spillover-lane-down is safe.** If `:8767` is unhealthy, fall back to the
   Anthropic queue (rule 3); never fail the request or trip.
6. **Observability.** A counter `anthropic_spillover_total{to=kimi}` + a log
   line per spillover decision (which account states triggered it).

## Honest limit (stated in the goal, not overclaimed)

"Exactly 100% + zero trips" is unattainable — Anthropic publishes no fixed
budget and the `allowed_warning`→`rejected` boundary is noisy. Verifiable
target: **≥0.95 binding utilization at a near-zero `rejected` trip rate,
spilling the residual.** The acceptance bar (invariant 1) is "zero trips on a
simulated all-`allowed_warning` fleet," not "exactly 100%."

## Out of scope (explicit)

- Per-request predictive scoring (FrugalGPT/RouteLLM cascades) — overkill for a
  2-tier Anthropic/Kimi fleet; the `allowed_warning` signal is the trigger.
- Spillover to providers beyond Kimi (DeepSeek/Gemini) — add later via the same
  mechanism if Kimi capacity itself caps.
- Changing the client-facing model id (claude-code still sends `claude-*`; the
  remap is proxy-internal).
