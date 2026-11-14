# UX tooling map — Throttler dashboard

Inventory: [`inventory.json`](./inventory.json). Scope and receipts: vault `1. Projects/Throttler/evidence/ux-ui-studio-2026-10-30/tooling-result.md`.
Discovery/status only. Impeccable owns page navigation. Architecture stays aiohttp + Jinja + HTMX 1.x; Catppuccin Mocha tokens only.

| Task | Skill / MCP | Invocation | Acceptance |
|---|---|---|---|
| Audit a dashboard view (hierarchy, a11y, anti-patterns) | Impeccable `audit` (pi skill 4.7.0) | `/skill:impeccable audit --target <template>` | Findings list with file:line; re-run shows fewer findings; no template edits outside the approved scope |
| Critique visual hierarchy / verdict | Impeccable `critique` | `/skill:impeccable critique` | Written critique; hero facts keep `fit-content(60%)` rule and unknown/stale/hold truth |
| Polish spacing, type, states before merge | Impeccable `polish` | `/skill:impeccable polish` | Diff limited to CSS/template; HTML validates; `lectrice`-style e2e not required here |
| Clarify copy for an error/hold state | Impeccable `clarify` | `/skill:impeccable clarify` | Copy keeps quota vs eligibility vs connectivity distinct |
| Quieter / bolder / simplify a panel | Impeccable `quieter` / `bolder` / `distill` | `/skill:impeccable <verb>` | Before/after screenshot via server browser route |
| Adapt for narrow viewports | Impeccable `adapt` | `/skill:impeccable adapt` | Server-browser acceptance at 390/768/1280/1919 CSS px; distinguish native 200% zoom from narrower-viewport reflow |
| Live DOM iteration on the running dashboard | Impeccable live (`scripts/live-browser.js`) | Requires an attached page on the server browser route | NOT EXECUTED in this slice |
| Attach to the running dashboard / web research | `browser-fleet` MCP (codemode) | Discover identities, inspect status, then use supported attach/run for the selected identity | Check `structuredContent`: `isError !== true`, `ok === true`, `refused !== true`. Missing/failed envelopes are HOLD, never healthy. Public research uses an anonymous search identity |
| Server health for a fleet dashboard check | `browser-fleet` `server_health` | `tools.mcp__browser_fleet__server_health({})` | Read-only; no deploy calls |
| Design-direction presets | `design-presets` skill | `/skill:design-presets <preset>` | Preset applied to tokens only |
| Redesign an existing UI | `redesign` skill | `/skill:redesign-existing-projects` | Functionality unchanged; test suite green |
| Image-to-code / design reference | `image-to-code`, `imagegen-web` | `/skill:image-to-code` | Needs image generation provider route; check funding before use |
| Architecture or flow diagram | `diagram-design` | `/skill:diagram-design` | Renders; saved under docs/ |
| Brand board | `brandkit` | `/skill:brandkit` | Not used for the dashboard without owner approval |

## Rules

- Do not auto-run every skill. Pick one verb per task.
- Python browser clients must use the installed `browser-patchright` runtime, which includes `browser_automation.humanize`; an arbitrary project venv may not. Keep policy imports and refusal checks intact. A successful import is not rendered acceptance.
- Do not hand-edit `~/.pi/agent/mcp.json` (Nix-managed).
- Do not add a second Impeccable install. Known drift: Claude plugin 4.5.0 (install cache missing) vs Pi 4.7.0. Owner: Nix/Claude config.
- Impeccable upgrade (update-check says 4.8.0) needs supply-chain review first. The launcher downloads binaries from GitHub when none is cached.
- Not configured: Figma/design-token MCP, automated contrast checker, visual-regression tool. Each needs review and credentials before wiring.
