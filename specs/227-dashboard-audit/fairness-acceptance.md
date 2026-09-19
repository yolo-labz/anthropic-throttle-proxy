# Fairness acceptance — PR #227

Code baseline: `83d8b54`. This is an acceptance checklist, not a receipt that the live dashboard serves this candidate; evidence must match the eventual delivered source.

Hypothesis: every dashboard claim is fair only when its scope and provenance are visible, and every rendering-only claim survives the live Firefox matrix.

Markers refer only to the Codex scorecard at `4eb8c28`, not an approval of `83d8b54`:
- **code-proven** — that scorecard marked the finding Addressed; this historical label is not whole-diff approval because the candidate includes OpenAI-authored code.
- **needs-live-firefox-gate** — the scorecard left the finding Partial/Not addressed.
All 17 findings still require acceptance against the delivered source. `verify-live.py` requires BOTH Firefox and Chromium at every listed width/zoom, not Firefox alone; the mixed-family source gate is also still blocked.

## Finding gates

1. **Pacing-clear is not credential/admission-clear — code-proven.** With pacing clear but credential evidence missing, the strip must show `UNKNOWN`; with a refusal it must show `CRIT` and the refused/disabled reason. It must not show `HEALTHY` or imply that pacing clearance proves admission.

2. **DNS is not egress/auth/inference success — needs-live-firefox-gate.** Each diagnostic row must show exactly `DNS resolved`, `DNS failed`, or `DNS unmeasured`; an omitted signal must render `unmeasured`. The page must not turn missing evidence into failure or present DNS as authenticated inference success.

3. **Complete custom-host identity — code-proven.** A custom hostname, literal IP, IPv6 address, and malformed-URL fallback must each remain fully readable in its row. The page must not shorten an unknown destination into an ambiguous fragment.

4. **Registry is not routing eligibility — needs-live-firefox-gate.** The banner must say `Registered providers` and `catalog membership, not current eligibility`; configured hidden families must be absent from the board. It must not say registered entries are enabled, permitted, or currently usable.

5. **Observed plan survives YAML captions — code-proven.** A row with an observed plan must keep that value and show a differing configured value separately as `Configured caption`; a row without an observation must say `not observed`. The caption must never masquerade as a measured plan.

6. **Pool limits are not account-wide exhaustion — code-proven.** A mixed-pool row must show `pool limited` and state that model applicability is unknown, while a same-pool 5h/7d constraint may still bind. The page must not claim the entire account is exhausted from one limited pool.

7. **Request counters are process-local — needs-live-firefox-gate.** The section heading must say `Local proxy · process-local counters`, the verdict detail must name the local view, and subscription-only mode must omit local signals/counters. The page must not describe these counters as fleet-wide or native-subscription totals.

8. **Capacity and recommendations require evidence — needs-live-firefox-gate.** `Subscriptions` must precede diagnostics; a binding card may appear only for measured refusal/pacing evidence, and `takes traffic next` may name only a measured `ok` row with real meters. If alternatives are unseen or unmeasured, the page must show no measured recommendation—not claim they can take traffic or that every sibling is blocked.

9. **Cadence, freshness, and refresh failure are explicit — needs-live-firefox-gate.** The meter report must show observation time, age, declared sampling interval, and expected next sample only when derivable; missing/invalid/future evidence must show unknown or stale/untrusted without a guessed cadence. In Firefox the 2s `as of` stamp must advance while connected, stop on disconnect to expose last-known data, and a build/display change must reload without stealing focus; the page must not silently look live after refresh failure.

10. **Meter scope/reset/pace have context — code-proven.** Every meter must retain its raw scope and duration, show the absolute UTC reset beside the countdown, and label pace/exhaustion as estimates. The page must not present a projection as guaranteed capacity or hide the pool scope.

11. **Missing, refused, and unconfigured are distinct — needs-live-firefox-gate.** Live rows must separately render `no reading`/unknown, refused, unconfigured, and stale/untrusted states with their source or error reason. The page must not infer funding, usability, or enablement from an absent or untrusted reading.

12. **Settings summary is derived and focus-safe — code-proven.** The visible settings and override totals must match the rendered knob inventory after refresh, an active settings control must retain focus/value, and a disabled advisor must be labelled disabled. The page must not show hardcoded counts or imply an unavailable advisor is active.

13. **Headings remain painted — needs-live-firefox-gate.** Firefox screenshots before scroll, after scrolling each table, and after HTMX refresh must show every section heading painted, unobscured, and attached to its section. A heading that disappears, overlaps, or repaints blank fails.

14. **Label, identity, and ID do not collide — code-proven.** Where all three exist, the account label must be primary and the authoritative identity plus local ID must remain readable on separate secondary lines. The page must not concatenate or visually collide them into one ambiguous string.

15. **Hierarchy stays legible across widths and zoom — needs-live-firefox-gate.** At 390/768/1440/2210 px and 100%/200% zoom, Firefox must keep subscriptions visually primary, headings and metadata legible, content unclipped, and page-level horizontal overflow absent while table wrappers remain scrollable. Tiny secondary text, flattened hierarchy, or clipped controls fail.

16. **Duration/account iconography is consistent — code-proven.** Each row must use `⏱️` for 5h and `📅` for 7d, show Codex A/B/C as plain badges beside a neutral family icon, preserve raw labels for unknown periods, and honor explicit custom icons. Boxed account glyphs or row-to-row duration-icon changes must be absent.

17. **Acceptance evidence is row-scoped — code-proven.** For every stable `data-subscription-id`, the same visible row must contain its expected identity, observed/configured plan provenance, duration icons, and visibility state. An icon or label found only in another row must not satisfy the finding.

## Cross-family adversarial review — 18/09/2026 (GPT-6 Astra)

The gate this document says was outstanding. Findings, all reproduced by the
reviewer against local HEAD `3d467e0`, and their disposition:

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | MAJOR | `_attach_binding` recommended an account whose own usage call answered `credential rejected (401)` — `_account_status` files an endpoint failure as a note and keeps the verdict `ok`, so the 12% beside it was never actually readable | `routing_eligible` gate on the account row (fail-closed: absent = no); a sibling that exists but cannot be verified now renders "no verified alternative", and "every sibling is blocked too" is reserved for siblings that really are closed |
| 2 | MAJOR | A PACING binding — a window upstream still reports as `allowed` — rendered as `blocked` with a `reopens in` | Wording follows the measured `evidence` already on the object: `throttled` → blocked/reopens, `pacing` → binding constraint/resets in |
| 3 | MAJOR | `data-revision` / `data-local` were inert; a tab open across a deploy kept old CSS/header/settings while receiving new markup | `hx-vals` on `#stats` reports the revision each tab was rendered with; `stats_partial` answers `HX-Refresh` on mismatch |
| 4 | MAJOR | `math.isfinite()` raises `OverflowError` on `intervalSeconds: 10**400` in the report JSON, crashing both dashboard endpoints | `lanes._pct` classifies an unrepresentable number as invalid instead of raising |
| 5 | MINOR | `_collect_view` handed the DISPLAY-PROJECTED view to the advisor, so `show_primary: false` made it diagnose an empty fleet | `_collect_view(project=False)` for the advisor; projection is applied in rendering handlers only |
| 6 | MINOR | `now - cached < TTL` is true for a negative elapsed time, so a backward clock step served a stale snapshot without re-reading | Bounded cache window `0 <= elapsed < TTL` |

Clean on review: deep-copy isolation, gauge-before-display ordering, explicit
credential refusals reaching the verdict, and ordinary missing/bool/NaN/infinite
cadence inputs all failing closed. Row-scoped icon/plan assertions verified real;
the July/August regression checks still exercise their named cases; no SDK,
token-logging, script-count or palette violation.

The reviewer could not certify the OpenAI-authored portion of the diff, and
browser acceptance at the widths below is recorded in the PR rather than here.
