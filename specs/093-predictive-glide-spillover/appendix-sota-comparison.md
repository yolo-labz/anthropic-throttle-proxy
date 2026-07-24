# Spec 093 — Appendix: build-vs-buy comparison against the SOTA

**Date:** 2026-07-23. **Method:** SearXNG-backed multi-agent research (ccr /
LiteLLM / native claude-code gateway / OpenRouter, from official docs + the live
GitHub issue trackers) + an 8-dimension impartial judge panel scoring OUR fleet
against best-in-class SOTA, synthesized by an Opus judge. This appendix records
WHY Spec 093 (extend-own-proxy + `:8760` unified ingress) is the correct
SOTA-competitive bet rather than adopting ccr / LiteLLM / OpenRouter.

## Scored matrix (ours vs SOTA, 0–10)

| Dimension | Ours | SOTA | Verdict |
|---|:--:|:--:|---|
| Rate-limit intelligence (AIMD, 5h/7d OAuth-window pacing, budget-vs-concurrency 429 split) | 9 | 7 | **ours leads** |
| Security & supply-chain (proxy-owns-key, Nix-pinned first-party binary, no npm/pip malware surface) | 9 | 6 | **ours leads** |
| Reliability / blast-radius (HA: health-check bypass-to-direct, socket-activated restart-in-place) | ~9 | ~5 | **ours leads** |
| Family-diversity / adversarial-review integration (Chinese generator → Western reviewer) | ~9 | ~2 | **ours leads** |
| In-session cross-vendor switch (no relaunch) | 7 | 8 | behind today → parity at `:8760` |
| Auto-failover across providers on cap | 7 | 8 | behind today → **lead** at `:8760` |
| Observability / metered-$ cost control | 7.5 | 8.5 | behind (metered-lane $ only) |
| Model-routing breadth (longContext/webSearch categories) | 7.5 | 8.5 | behind today → superset at `:8760` |

Fleet-weighted (rate-limit + security are the load-bearing axes for a ~50-tab
flat-rate-Max fleet): **ours 8.1 vs SOTA 7.4 today**, widening after `:8760`.

## Where we structurally lead (SOTA cannot copy)

- **Rate-limit intelligence.** True AIMD (multiplicative shrink / additive grow)
  vs ccr's fixed `retryCount` backoff; Anthropic 5h/7d OAuth rolling-window
  pacing + auto-pause (unique — LiteLLM tracks RPM/TPM but not Anthropic's
  window shape); explicit budget-vs-concurrency 429 split (no SOTA tool
  disambiguates). Provider-agnostic gateways are the wrong shape to encode
  Anthropic-specific quota physics.
- **Security & supply-chain.** LiteLLM shipped a 1.82.7/1.82.8 supply-chain
  malware advisory; ccr is single-maintainer with a documented live-crash class
  (gateway stream-abort, model-too-long) + no HA + an Electron pivot; OpenRouter
  is a 3rd-party data path carrying prompts AND keys with markup. Ours is a
  single Nix-pinned (rev+SRI) first-party binary, proxy-owns-key on
  `:8765`/`:8767`, keys from sops+rbw, fully self-hosted, plus the
  family-diversity review gate none of them ship.

## The real gaps (all trace to one missing piece: the in-band router)

- **Gap A — no cross-vendor auto-failover today.** Hitting the 7d cap needs a
  manual relaunch into `claude-zai`/`claude-kimi`. **Closed by `:8760`** (gauge
  spill, never-hard-fail-while-a-lane-serves). Optional interim: `503-on-7d-cap
  → transparent GLM egress for bulk roles only` in `:8765`.
- **Gap B — relaunch-to-switch.** `/model` works only within a lane. **Closed by
  `:8760`** (one base_url, model-field routing → in-session lane switch).
- **Gap C — metered-$ visibility (residual).** Generalize the Kimi low-balance
  gauge to a per-metered-lane credit gauge (GLM+Kimi) + Alertmanager on the
  existing Grafana.
- **Gap D — no OSV-Scanner/Scorecard on THIS repo (residual, the one hole in the
  security lead).** Every other yolo-labz plugin mandates them. **CLOSED by the
  PR carrying this appendix** (adds `osv-scanner.yml` + `scorecard.yml`;
  attestation + CycloneDX/SPDX SBOMs already existed in `attest.yml`).
- **Gap E — manual-override ergonomics thinner than ccr even at `:8760`.** Add a
  deterministic `provider,model` passthrough on `:8760` on top of the
  role-inferred default (gets ccr's manual override AND our HA/anti-degradation
  invariants).

## Final call

**Build, don't buy.** Adopting ccr/LiteLLM/OpenRouter would trade away our two
highest-weight wins (rate-limit depth + trust) to buy breadth we've already
designed and dated (`:8760`, per this spec) — and both dominant OSS gateways
carry live risk you'd be putting in the trust boundary of ~50 tabs holding
prompts + Max OAuth creds. Borrow their *ideas* (credit gauges, model-id
passthrough), not the tools. Ship `:8760` on schedule; the only thing genuinely
missing is a calendar date already set.
