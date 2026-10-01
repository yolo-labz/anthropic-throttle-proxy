# Team API contract corrections — coordinator evidence

Private authenticated verification established these protocol facts; the examples below are synthetic and contain no account identifiers, credentials or actual quotas.

1. Team `currentPeriodEnd` uses **`YYYY-MM-DDTHH:MM:SS`**, unlike the legacy individual response's space-separated form. The assignment's generic example used a space; that example was not a verified Team wire format. Team parsing must accept the observed T-separated form (retain individual compatibility).
2. A valid currently assigned seat can have **`historyCreditsUsed < creditsUsed`**. Do not require history >= current, and do not make this unrelated historical display field a prerequisite for current/total quota. Use only the authoritative current `creditsUsed` and `creditsTotal`; history semantics are not established. Synthetic valid example: current=120, total=1000, history=0, periodEnd=`2030-02-01T23:59:59`.
3. `page.goto(plan-manage)` emits individual/profile reads, not the Team My Seats response. Team telemetry must explicitly perform the bounded read-only Team request AFTER validating browser identity (or navigate the actual Team/My Seats UI). Merely adding the Team route to a passive listener and waiting will always publish unknown. The configured project ID must be strictly validated before constructing the path; no raw-key endpoint is needed or allowed.

Current WIP violates all three. Add focused synthetic tests covering the observed format, lower/missing historical display counters, and a fake browser that emits only legacy requests on navigation. Preserve credential-free failure reporting. This is not permission to use browser, rbw or private account material in the worker; coordinator owns live validation.
