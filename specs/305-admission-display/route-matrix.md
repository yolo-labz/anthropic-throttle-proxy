# 305 — intended client/ingress route matrix (source-only)

Derived from the code at this exact head only. **This matrix (and the project
receipt) is the ONLY place a client-path fact may live** — the runtime display
never carries it: `ui/routes.py` has no "client path not accepted" state, no
verdict for it, and no parameter to accept one. The display's sole new source
is the existing local admission predicate (`bearer_usable` ∧
¬`credential_dead` ∧ `meter_binding_allows`) feeding the EXISTING status
vocabulary.

## Routes — honest distinction

| # | Caller | Route | Ingress? | Downstream | Notes |
|---|--------|-------|----------|-----------|-------|
| C1 | mimo-desktop seats (real clients) | `POST /v1/chat/completions` **directly on the MiMo proxy** | **no — direct route** | MiMo Token Plan / Team seats (`tp-…` static keys) | No spec-093 ingress is in this path; the proxy is the whole route. Local admission here is local capacity only. |
| C2 | claude-code / Anthropic SDK | `POST /v1/messages` **directly on the Anthropic proxy** | **no — direct route** | Anthropic OAuth / api-key accounts | `ok:False` verdicts render `CRIT refused/disabled`; positive admission never revives them. |
| I1 | ingress (spec 093) | role chains `generate`/`judge`/`bulk`/`code` over `anthropic, deepseek, kimi, glm, codex` | **yes — ingress** | per-lane proxies | Registry policy is removal-only (`provider_registry.load`). The ingress's verdicts are its own; a proxy's status strip never claims to speak for the ingress or the fleet. |
| X1 | control reads | `GET /ui`, `/__throttle/health`, `/__throttle/admission` | no | none (local) | Render path performs no network I/O beyond the display's pre-existing collection. |

## Display semantics (unchanged vocabulary, worst-wins)

1. `CRIT · N of M bearers refused/disabled — inference unavailable on those
   lanes` — `ok is False` or admission `False`. Never revived.
2. `THROTTLED` / `PACING` — pacing signals.
3. `UNKNOWN · capacity unknown — no auth/permission evidence …` — no positive
   evidence at all.
4. `HEALTHY · all N bearers clear` — positive evidence (credential `ok: True`
   or positive local admission, keeping its existing local meaning).

## Count scope (true scope)

`N of M` counts the bearer set this proxy's status projection actually
tracks — `bearer_state` minus the `_anon` bypass slot, exactly the list
`_compute_status` receives — and the strip suffixes `· local proxy view`.
Display hiding (hidden families) filters RENDER only and never the status
counts. It is never a fleet-wide or native-subscription count.

## Direct MiMo client-path acceptance — named artifacts & conditions

What an end-to-end acceptance of route C1 requires, named exactly. Until
ALL conditions hold in ONE receipt, account B stays `client-path-unaccepted`
as the receipt/matrix fact above — no runtime display state represents or
accepts it before or after.

**Named artifacts** (protected/project lanes; none of this ever enters
runtime state or public output):

1. `client-path-acceptance receipt` — the project receipt for route C1,
   owned by the protected lane (pF), recording the acceptance attempt.
2. The sanitized account-map entry for B — labels and booleans only; never
   tokens, emails, account ids or meter-binding lane ids.
3. This matrix's C1 row — updated with the acceptance date and the receipt
   reference in the same change (docs-only).

**Named conditions** (all must hold together in that receipt):

- **C1.1** a REAL mimo-desktop client request traverses route C1 end to
  end (direct: client → MiMo proxy → upstream) — not a probe, not a
  synthetic message, not an ingress or relay path;
- **C1.2** the upstream answer is a genuine completion delivered to that
  client (HTTP 200 with a real completion body) — never a local synthetic
  response;
- **C1.3** the request runs on the seat bound in the sanitized map (the B
  seat), recorded under its sanitized label only;
- **C1.4** the attempt falls inside the window the project receipt names —
  the receipt owns the window; this matrix invents no duration;
- **C1.5** acceptance is never inferred from local admission, served
  counters, meters or provider-registry membership — those remain local
  facts with local meaning only.

**After acceptance:** the ONLY change is the receipt plus this matrix. No
runtime parameter, verdict or state in `ui/routes.py` accepts, carries or
represents client-path acceptance (pinned by
`tests/test_admission_display_trusted.py`).

## Project receipt fact (static, never runtime)

Per the project receipt, account B currently has a real meter binding and
positive local admission, while its real-client path is **not accepted**.
That fact lives in this matrix and the receipt only; the runtime display
renders B through the existing local admission meaning above, with no
invented verdict and no B-unknown regression. When the project receipt
records acceptance, only the receipt/matrix change — no runtime state is
required or accepted for it.
