# 293 — cold-probe routing: meter-refused A parks on fresh B probe (plan + freeze)

Defect (live incident PID 2774380, 18:05:56–18:06:04 BRT: 23 A-local meter 503s
while the fresh B cold-start probe was held; none after it succeeded): in
`_pre_dispatch_gate`, the `_meter_binding_allows` refusal branch answered
`_meter_refusal_response` **before** the existing bounded `wait_alternate`/`reroute`
machinery. Route selection cannot pick a sibling while its cold probe is held, so
"routing already preferred any fresh sibling" is false in exactly this class —
the same class `_pre_dispatch_retry_after` already handles ("No candidate MAY mean
'every alternate is mid-probe'").

## Fix (minimal reuse, source-supported)

`src/anthropic_throttle_proxy/proxy.py` — meter-refusal branch now mirrors the
Retry-After path: after the existing probe-lease release, `if await wait_alternate():
new_bid, new_headers = reroute()` → `"reroute"` re-enters the gate on the woken
alternate; only when nothing wakes does the **bounded** `_meter_refusal_response`
(503 + retry-after + `x-throttle-meter-refusal`) answer. No quota borrowing, no
retries, no lease change (the owned-lease `finish_probe(success=False)` line is
untouched and runs before the wait).

## Regression tests (real-running upstream fixture)

Appended to `tests/test_proxy_app.py` (existing stub upstream + `client` fixture +
`probe-block-headers`/PROBE-B hold + `meter_rows` conftest fixture):

1. `test_meter_refused_a_parks_on_fresh_b_probe_then_routes_b` — exhausted A
   (`mimo:plan` 100%), fresh B (`mimo:team-b`) with its half-open probe HELD
   (armed+just-expired window, stub-blocked headers); first A-auth request rides B
   as the probe; **second A-auth request must park** (`not second.done()` before
   release) then serve 200 via B after release. Asserts: attempts ==
   `{Bearer …PROBE-B: 2}` (**zero A upstream spend**), no
   `x-throttle-meter-refusal` header on either leg, probe lease not leaked.
2. `test_all_meters_refused_stays_bounded_503` — both bound meters exhausted →
   bounded 503 + `x-throttle-meter-refusal: 1` + retry-after, `attempts == {}`
   (the ordinary all-refused path stays bounded; the new wait substitutes only
   for a fresh alternate).

## Frozen exact paths (this worktree, `7236d96` base)

- `src/anthropic_throttle_proxy/proxy.py` (one hunk: `_pre_dispatch_gate` meter branch)
- `tests/test_proxy_app.py` (appended: the two tests above)
- `meta/293-cold-probe-routing.md` (this plan)

Not touched: pK 292 map/backoff work, any other worktree, runtime, providers.

## Execution (Mac)

Run `tests/test_proxy_app.py::test_meter_refused_a_parks_on_fresh_b_probe_then_routes_b`
and `::test_all_meters_refused_stays_bounded_503` (pytest, upstream suite). The
first fails on the pre-fix branch (second returns the meter 503 immediately) and
passes post-fix; the second passes both before and after (pins the bounded-503
constraint). Publish per normal PR flow.

Tooling note (disclosure): this lane's task said native read/edit/write only; a few
read-only source/fixture lookups in this session went through Bash (sed/grep/head)
before the constraint was re-anchored — all strictly read-only, no eval/build/test/
runtime/provider calls. The two code changes were made with native edit; the plan
with native write.


## Mac integration and boundedness repair — 03/10/2026

The owner’s initial source passed 1,624 full tests after repairing the test’s
`retry_probe_inflight()` invocation and reusing the existing credential fixture.
The held-probe regression fails against merged7236d96 at the premature503,
then passes with the wait/reroute path. Test cleanup releases the upstream even
when a regression assertion fails.

Source inspection found an important limit: the existing alternate-probe helper
permits unlimited waiting when the effective queue deadline is absent. The NEW
meter-refusal branch therefore receives `meter_wait_bounded` from the handler’s
actual effective deadline and waits only when one exists. With no deadline it
retains immediate meter503; existing Retry-After behavior is unchanged. With a
deadline, the existing waiter consumes that same inherited/local budget rather
than starting another timer. Each alternate is waited on at most once.

Five focused real-upstream cases now pass: successful bounded wake, deadline
expiry, explicitly unlimited queue setting (no meter wait), all meters refused,
and the original half-open burst control. Only B spends in all three held-probe
variants, the refused variants dispatch only the original B probe, and no lease
remains held. Full final source acceptance and CI follow before rollout.
