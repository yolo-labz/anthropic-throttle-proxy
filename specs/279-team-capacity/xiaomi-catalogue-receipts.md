# Xiaomi integration catalogue — verification receipts (T12/T13 + meter catalogue)

Recorded 02/10/2026 23:30–23:55 BRT by the integration coordinator seat.
Scope: bounded read-only verification + public-code receipts. No concurrency
raise, no purchases, no service/fleet restart, no provider bypass. Private
account/billing evidence stays with pF.

## Runtime under test

- :8773 build: `/nix/store/q939avydrfnq12bp79v87lps460bmmdh-anthropic-throttle-proxy-0.1.0`
  (post-#282/#283 deployment; includes #280 telemetry/UI and #283 plan-meter
  scoping + retired-reserve draining).
- Health at capture: served 962, inflight 12, queue_mode fair, 20 slots,
  egress ok. `drain` estimator live and `evidenced: true` (spec 221 T8 shipped).

## T12/T13 — spec 221 (zai queue-depth admission)

- **T12 — PASS (already landed).** `tasks.md` marks PR merged; the drain
  admission estimator is present and evidenced in the deployed proxy (`drain`
  block with measured service time, `admits:true`, `rejects:false`).
- **T13 — recorded in `fleet-intel` this session:** synthetic acceptance
  complete (T5–T11 green), **live verification blocked by closed Z.AI
  admission** — replay against `/api/coding/paas/v4/chat/completions` stays
  honestly unclaimed (issue #238 item 4 same blocker).

## Xiaomi integration catalogue — three checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | **Meter/bearer binding** | **PASS with gap** | Hash binding verified locally: slot B key (`~/.pi/mimo-token-plan-b.key`) → `sha256("Bearer "+key)[:8] = 07c7ae8a` = the **serving** bearer (388 served, cap 20) — the second Team seat, assigned today by the account owner lane. Slot A → `6994af7a` (owner Team seat, AIMD-shrunk to 2 after a 429, cooldown). Retired individual → `973b86a2` (matches known). **Gap:** the report has a row only for the owner seat (`mimo:team-owner`, exhausted 100.13%) and the individual (`mimo:plan`) — no meter row exists for the serving second seat, so the UI cannot show the one actually usable allowance. Catalogue needs one row per assigned seat. |
| 2 | **Sampler freshness** | **FAIL — stale (root cause found)** | `mimo-plan.json` `generatedAt=2026-10-02T23:33:16Z`, checked 03/10 02:26Z: age **~2 h 53 min** against the 900 s cadence. Probe journal shows the concrete cause at 23:03 and 23:18: **`cat: write error: No space left on device`** — the sampler cannot write its temp file. **Storage recovery is already owned jointly by pJ + Ops p6 (CI/storage); this seat does not compete.** Once space returns, verify the 15-min cadence resumes before trusting either lane row. |
| 3 | **Exhausted-lane exclusion** | **FAIL — inconsistent status** | `mimo:plan` shows `usedPercent=100.044` (fully used) but `status: "ok"`; `mimo:team-owner` shows `100.132` and correctly `status: "exhausted"`. The individual-plan `report()` marks exhausted only on `expired` or period-end, not `used >= total` — an exhausted lane renders as healthy. The UI must not present it as usable capacity, and routing must keep excluding exhausted/retired keys (spec 283 groundwork landed in #283). |

## Follow-ups with original owners (no ownership change)

- **pH (sampler):** after pJ/p6 restore disk space, fix the individual-plan
  exhausted condition (`used >= total` → `exhausted`) to match the team row;
  add one seat-B meter row (per-assigned-seat catalogue); synthetic regression
  for both.
- **pK (routing):** serving bearer is slot B (`07c7ae8a`); slot A (`6994af7a`)
  is 429-shrunk and must keep attracting no new load while B has free slots —
  confirm end-to-end with #283 semantics after the sampler is truthful.
- **pJ + Ops p6 (CI/storage, already active):** `No space left on device` is
  the sampler-stale root cause — this seat does not run competing reclaim.
- **pM (tab preservation):** untouched by this verification; the one observed
  quota 429 (model p3 class) recovered on next tools — provider not down.

No blind concurrency raise performed. No extra purchases. Second Team seat
remains unassigned and is not counted as capacity.
