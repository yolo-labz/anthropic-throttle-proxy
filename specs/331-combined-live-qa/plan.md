# Combined live QA shipping continuation — 07/10/2026

Hypothesis: the accepted c4bd3ce reader on pV's new :8765 build accounts only
Pi-journal output over [sample−60,sample), and the real UI preserves that combined
counter through source changes/HTMX without losing geometry/accessibility.

Falsifiers: old95 build, differing reader/UI/CSS bytes or runtime/persistent
command; native-reader versus independent fixed-window sum mismatch; displayed
rate impossible in the real cache-age window; selector-dependent fingerprints;
missing/partial/stale journal shown healthy; AX/geometry/focus/network failure.

## Sequence

1. Freshly inspect actual PR328 head/base/required checks/threads, normal protected
   squash and verify MERGED. Preserve dirty327/325 WIP and all historical failures.
2. Fresh isolated331 slice from current main; no backend/CSS/auth/provider/device
   or lifecycle modifications. pV alone activates its accepted-c4bd3ce one-service
   build. Metadata may run in control; tests/rendering require heavy admission.
3. Reuse prior exact-byte/deployed Chromium oracles. Extend QA only: run deployed
   reader with the actual service interpreter against its actual journal; compare
   at the returned fixed timestamp with a separately parsed bounded journal sum.
   Keep raw records in memory only, publish aggregate/timestamp/provider coverage.
4. Real service HTML does not expose its cached sample endpoint. Do not invent it:
   bracket possible sample endpoints by the real15s cache validity and verify the
   displayed rounded rate is achievable by a common60s journal window. State this
   limitation separately from exact native-reader arithmetic.
5. Read-only local→sibling→local triplet must finish within2s and the middle gauge
   fingerprint must equal one bracket (allows one natural5s collector tick, no
   retries/cache freezing). Browser both widths: real HTMX/source persistence,
   keyboard/touch/focus/details, actual quota/AX, visible gauge, no unexpected
   requests. No synthetic patches or inference in live mode.
6. Guard import root and accepted hashes before/after. Save successes, failures and
   pre-execution refusals separately. Update UI-TEAM in the same turn. Deliver
   tests/receipts via normal hooks and exact-head CI; safe squash when green.

Deliverables: docs/COMBINED-LIVE-QA-2026-10-07.md and
 docs/evidence/combined-live-qa-2026-10-07/. No scratch-only knowledge/artifacts.
