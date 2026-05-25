# anthropic-throttle-proxy · Brand Spec

> Captured: 2026-05-22
> Source: grown from the shipped `/ui` dashboard (Catppuccin Mocha + JetBrains Mono).
> Asset completeness: complete (mark, favicon, lockup, palette, type).

The identity is deliberately *not* a generic "AI tech" look. The mark **encodes
the product's behaviour**: the AIMD congestion-control sawtooth — additive
increase, multiplicative decrease — which is the literal algorithm the proxy
runs on every bearer.

## 🎯 Core assets

### Mark — AIMD Sawtooth
- File: `assets/brand/logo.svg` (100×100 tile, dark base)
- The teal line is the AIMD live-ceiling over time: slow ramps (additive
  increase), sharp drops (multiplicative decrease ×0.7). The dashed top line is
  the hard ceiling (`hard_max`); the solid bottom line is the floor
  (`THROTTLE_AIMD_MIN`). The two peach dots mark pushback events (429/503).
- Usage: README hero, repo social preview, slide decks, app icon.
- Don't: recolor the line off-teal, fill the curve, add a gradient/glow, or
  rotate the tile.

### Favicon
- File: `assets/brand/favicon.svg` (also served at
  `src/anthropic_throttle_proxy/ui/static/favicon.svg`, linked from the `/ui`
  dashboard `<head>`).
- Simplified mark: thicker stroke (8), no ceiling/floor guides, no dots — stays
  legible at 16px.

### Lockup
- File: `assets/brand/lockup.svg` (mark + wordmark + tagline, 560×122)
- The wordmark colours the verb: `anthropic`/`proxy` in subtext, **`throttle`**
  in teal — the product *is* the throttle. Hyphens in overlay0.
- Tagline: `fleet-wide pacing in front of api.anthropic.com`.

## 🎨 Palette — Catppuccin Mocha

| Role | Token | Hex |
|------|-------|-----|
| Background | base | `#1e1e2e` |
| Surface | mantle / crust | `#181825` / `#11111b` |
| **Brand + primary** | **teal** | **`#94e2d5`** |
| Links | blue | `#89b4fa` |
| Healthy | green | `#a6e3a1` |
| Warning / pushback | peach | `#fab387` |
| Throttled / floor | red | `#f38ba8` |
| Ink | text / subtext0 | `#cdd6f4` / `#a6adc8` |

## 🔤 Type
- **Display / wordmark / data**: JetBrains Mono (fallback: Fira Code, ui-monospace).
- **Body / prose**: system sans (`ui-sans-serif, system-ui, …`).
- No web-font fetches — a self-hosted proxy ships no third-party font calls.

## Accent rule
- **teal** = brand + primary action only.
- **green / peach / red** = health state only (never decoration).
- Everything else is base/surface/text. No colour earns a place unless it
  carries meaning.

## Voice
- Operator-first, precise, lowercase, mono. States facts and numbers, not
  adjectives. "1 of 3 bearers throttled", not "uh-oh, rate limited!".

## Anti-slop guardrails (refuse on sight)
- No gradients, no neon glow, no glassmorphism.
- No rocket/sparkle/shield iconography.
- No raw hex in components — Catppuccin tokens only.
- The mark stays a single-weight line; never a 3D/extruded/“app-store” render.
