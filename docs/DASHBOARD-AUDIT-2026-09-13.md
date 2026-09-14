# Dashboard truth and presentation audit — 13/09/2026

Status: **implementation candidate; not delivered or visually accepted**.
This document contains source-level findings and synthetic examples only.
Operational screenshots, account records and deployment evidence stay outside Git.

Hypothesis: dashboard view builders conflate process availability, quota evidence
and display configuration, while discarding provenance needed to interpret meters.
Falsifier: the unchanged view builders preserve observed plans, reject untrusted
observation timestamps and cannot report HEALTHY for a refused credential.
The frozen acceptance suite initially failed eight of nine checks.

## Requirements and implementation trace

| # | Finding | Candidate / remaining acceptance |
|---|---|---|
| 1 | Pacing-clear is not credential/admission-clear | Status consumes collected credential verdicts; unknown/refused never become HEALTHY. Explicit display-only mode uses a neutral SUBSCRIPTIONS heading. Authoritative policy is not inferred. |
| 2 | DNS is not egress/auth/inference success | Provider view retains explicit boolean DNS evidence only; template says DNS, unmeasured when absent. |
| 3 | IP/custom-host identity was truncated | Preserve complete unknown host/IP labels, including IPv6 and malformed-URL handling. |
| 4 | Registry membership claimed enabled routing | Label registered providers, not eligibility; configured hidden families are absent from this display. |
| 5 | YAML caption overwrote observed plan | Preserve `plan`; expose `plan_caption` and `plan_conflict` separately. |
| 6 | Distinct pools were conflated with account-wide exhaustion | Mixed Codex pools are `pool limited`, with model applicability explicitly unknown. Same-pool 5h/7d constraints remain binding. |
| 7 | Local request counters appeared fleet-wide | Label process-local scope; hide this section in subscription-only mode. |
| 8 | Capacity required joining diagnostic tables | Subscriptions precede provider diagnostics. Eligibility remains unknown without an authoritative source; no routing recommendation is manufactured. Further actionable-capacity work is pending that source. |
| 9 | HTML polling hid telemetry cadence/failure | Show observation time, age, interval and expected next sample. Invalid/future timestamps cannot certify freshness. A server-rendered `as of` timestamp stops advancing when HTMX refresh fails; there is no client-side watchdog. Changed build/display mode requests reload; disconnect visibility and focus behavior remain browser gates. |
| 10 | Raw pool scope/reset/pace lacked context | Preserve scope and duration, show absolute UTC reset time, label pace as projection. Undocumented model mappings remain unresolved. |
| 11 | Missing/refused/unconfigured readings conflated | Preserve refusals; mark untrusted readings unknown and show source/error text. Do not infer funding or enable providers. |
| 12 | Hardcoded settings/override count | Derive both from `knob_snapshot`; refresh count without replacing focused settings controls. Advisor disabled honestly. |
| 13 | Suspected missing heading paint | Not reproducibly isolated. No speculative sticky-header removal. Real-browser before/after-scroll/refresh acceptance remains pending. |
| 14 | Identity/name/ID text collided | Secondary identity/ID placed on separate lines; authoritative identity retained. |
| 15 | Small secondary text and weak hierarchy | Increase heading/metadata legibility; prioritize subscriptions. Widths 390/768/1440/2210 and 200% zoom remain browser gates, not source-test claims. |
| 16 | Inconsistent duration/account icons | Same ⏱️ 5h and 📅 7d icons regardless of provider; neutral A/B/C badges replace boxed account emoji. Unknown periods retain raw labels. Explicit custom icons remain configurable. |
| 17 | Page-wide icon oracle could pass the wrong row | HTML assertions select stable `data-subscription-id` rows; visibility, identity, plan provenance and each row's icons checked together. |

## Display configuration

Existing YAML remains valid. Optional settings:

```yaml
defaults:
  hidden_families: [anthropic]
  show_primary: false
```

These are **display controls, not security or admission controls**. Hiding a
family does not stop UI telemetry collection or gauge publication; separate
credential automation, inference routes, other processes and network containment
must still be configured independently. Do not use visibility as a kill switch.

Configured `plan` is an annotation, not provider evidence. Missing observations
have an empty observed plan. A custom configured emoji remains honored except
for boxed Codex A/B/C glyphs, which become a neutral family icon plus a plain badge.

## Verification and provenance

`specs/227-dashboard-audit/verify.sh` runs source checks. The independent live
oracle additionally needs the exact running source, row-scoped live HTML and a
permitted browser's rendered acceptance; source tests cannot satisfy it.

Implementation provenance is mixed: GLM authored credential/provider truth and
plan-caption preservation; OpenAI integrated those candidates and authored the
remaining freshness/presentation code after bounded worker failures. A same-family
OpenAI review cannot certify the OpenAI-authored portion. Do not silently substitute
an unavailable reviewer, or claim an ungated candidate is reviewed/merged/deployed.
