# Fair capacity policy and measurement — workspace w1P

## Mission
Turn the user's requirements (consolidate limits, equalize eligible seat load, improve useful throughput/parallel connections) into a precise acceptance contract and a small executable, deterministic fairness/concurrency measurement using the existing limiter/router. No new scheduler framework.

## Ownership
Work only in `/home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy-282-capacity-contract`, branch `282-capacity-contract`, base `2e7a43f`. Own ONLY `specs/282-capacity-contract/` and one runnable benchmark/check `scripts/check-seat-fairness.py` if needed. Read public repo code/tests. Do not edit production code or siblings' tests.

## Required outcomes
1. Inspect existing `budget_paced`, `least_loaded`, FairBearerLimiter, plan meters and static credential pools. Specify the smallest missing contract, not a rewrite.
2. Explain equal eligibility-normalized utilization vs equal raw requests: exclude exhausted/stale/unknown/unassigned seats; preserve private/provider-family/model capability/reserved-lane restrictions. Do not add percentages with different windows, credits with money, or unassigned purchased capacity to active quota. No claim that monthly runway is proven.
3. Runnable synthetic check/measurement: equal eligible servers, asymmetric service durations/caps, unavailable seat, cancellation, client RR. Measure completed work/time, effective overlap, queue wait and distribution; assert the applicable existing invariants. Clearly label simulation, not provider benchmark. No real inference or account access.
4. Give falsifiable target conditions for safe cap/gap tuning and halt on 429/queue timeouts; a higher numeric cap is not proof of throughput.

Follow speckit spec→plan→tasks and keep this scope bounded. No browser, credentials, private Notes/runtime transcripts, deployment, external inference, new delegates or tabs. Preserve Z.AI reserved slots and existing Pi behavior. Run the check and normal lint. Commit your files only with hooks; no push/merge. Deliver exact SHA and executable results in `result.md`. Coordinator integrates to branch 279. Supersedes the unrelated editx assignment for this idle worker.
