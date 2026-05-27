# Anti-AI-Slop Checklist: /ui Dashboard (Spec 032)

**Source**: `/home/notroot/.claude/rules/anti-ai-slop.md` (Pedro's global)
**Audited**: 2026-05-27
**Implementation commit**: `1fd61b5`

Each item asks: does this anti-pattern appear anywhere in `dashboard.html`,
`partials/*.html`, or `style.css`?

## 2026 slop fingerprint (16-item ban list)

- [x] CHK001 Indigo→violet→pink gradient hero — `grep -E 'gradient|linear-gradient|radial-gradient' src/anthropic_throttle_proxy/ui/static/style.css` → 0 matches. PASS.
- [x] CHK002 100vh hero with centred headline + CTA pair — no hero in the file; `grep '100vh' style.css` → 1 match in `body { min-height: 100vh }` (full-page chrome, not a hero). PASS.
- [x] CHK003 3-column rounded-card feature grid with identical icon-on-top — N/A; `.cards` is auto-fit grid for metrics (counts, not features). PASS.
- [x] CHK004 Lucide/Heroicons stack — `grep -rE 'lucide|heroicon|sparkle|rocket|shield|lightning' src/anthropic_throttle_proxy/ui/` → 0 matches; only favicon.svg. PASS.
- [x] CHK005 Glassmorphism (`backdrop-blur`, `bg-white/10`) — `grep -E 'backdrop-blur|backdrop-filter' style.css` → 0 matches. PASS.
- [x] CHK006 Inter as the only typeface — fonts are `var(--sans) = ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif` + `var(--mono) = JetBrains Mono, Fira Code, ui-monospace, ...`. Inter NOT referenced. PASS.
- [x] CHK007 Pure black + neon purple glow (`shadow-[0_0_40px_rgba(168,85,247,0.5)]`) — base is `#1e1e2e` (Catppuccin Mocha base, not pure black); no `0 0 40px` glow shadows. PASS.
- [x] CHK008 Vertical accent borders on every callout — used INTENTIONALLY on `.status`, `.advisor-output`, `.advisor-auto`, `.config-row.overridden` only (4 surface classes, not "every callout"). PASS.
- [x] CHK009 Sparkle emoji / shimmer animations on CTAs — `grep -E '✨|sparkle|shimmer' src/anthropic_throttle_proxy/ui/` → 0 matches. PASS.
- [x] CHK010 Bounce easing — `grep -E 'cubic-bezier\(.*1\.[5-9]|bounce' style.css` → 0 matches; only `120ms ease-out` for transitions. PASS.
- [x] CHK011 Lorem ipsum / Acme Inc / Jane Doe — `grep -iE 'lorem|acme|jane doe|john@example|placeholder' src/anthropic_throttle_proxy/ui/templates/` → 0 matches. PASS.
- [x] CHK012 Stock testimonial blocks — N/A; this is an operator dashboard. PASS.
- [x] CHK013 Skipped heading hierarchy — h1 (brand) → h2 (sections) → h3 (advisor output). `grep -hE '<h[1-6]' src/anthropic_throttle_proxy/ui/templates/**/*.html` shows sequential. PASS.
- [x] CHK014 Cramped padding under generous margin — cards `padding: 0.85rem 1rem`, table cells `0.6rem 0.85rem`, advisor-output `0.9rem 1.1rem` — all balanced. PASS.
- [x] CHK015 Symmetric pricing table with `scale-105 ring-2` middle tier — N/A. PASS.
- [x] CHK016 One radius/shadow scale shared across button + input + card + avatar — DIFFERENT scales by surface class: input radius `3px`, button radius `6px`, card radius `8px`, badge radius `5px`. No uniform `rounded-2xl` slop. PASS.

## Augmentation rules

- [x] CHK017 Token-only colors (`ctp-*`) — `grep -E '#[0-9a-fA-F]{3,8}' style.css | grep -v '^:root\|--ctp-'` → empty; all raw hex inside `:root` token block. PASS.
- [x] CHK018 No arbitrary Tailwind values — N/A (no Tailwind). PASS.
- [x] CHK019 Sequential heading hierarchy — see CHK013. PASS.
- [x] CHK020 No 100vh hero sections — see CHK002. PASS.
- [x] CHK021 No three-icon-card grids on landing — see CHK003. PASS.
- [x] CHK022 Easing tokens only — `ease-out` / native springs only. PASS.
- [x] CHK023 Real content always — see CHK011. PASS.
- [x] CHK024 One radius + one shadow scale per surface class — see CHK016. PASS.

## Verdict

24 items audited, 0 violations. Anti-AI-slop gate **GREEN**.
