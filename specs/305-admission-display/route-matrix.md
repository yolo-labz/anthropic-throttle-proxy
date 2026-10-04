# 305 — intended client/ingress route matrix (source-only)

Derived from the code at this exact head only — no live routing claims, no
provider evidence. Companion to `contract.md`; the admission display for every
route below is the **existing local admission predicate** (`bearer_usable` ∧
¬`credential_dead` ∧ `meter_binding_allows`) and nothing else.

## Routes

| # | Caller | Route | Downstream | Bearer class | Display trust facts |
|---|--------|-------|-----------|--------------|---------------------|
| C1 | mimo-desktop seats (real clients) | `POST /v1/chat/completions` on the MiMo proxy | MiMo Token Plan / Team seats (`tp-…` static keys) | meter-bound (`mimo:plan`, `mimo:team-owner`, `mimo:team-b`) | binding + local admission are LOCAL evidence only; client-path acceptance needs a real message (`credential.ok: True`) or an explicit sanitized-map `True` |
| C2 | claude-code / Anthropic SDK | `POST /v1/messages` on the Anthropic proxy | Anthropic OAuth / api-key accounts | unbound (meter binding absent) | `ok: False` verdicts stay CRIT `refused/disabled` and are never revived by admission; `ok: True` (real message reopen) is client acceptance |
| I1 | ingress (spec 093) | role chains `generate`/`judge`/`bulk`/`code` over `anthropic, deepseek, kimi, glm, codex` | per-lane proxies | per-lane | registry policy is removal-only (`provider_registry.load`); local verdicts remain per-proxy ("· local proxy view"), never fleet-wide |
| X1 | control reads | `GET /ui`, `/__throttle/health`, `/__throttle/admission` | none (local) | n/a | render path performs no network I/O; verdicts are counts + fixed phrases, never account evidence |

## Display semantics (worst-wins order)

1. **`CRIT · N of M bearers refused/disabled — inference unavailable on those
   lanes`** — a credential verdict with `ok is False`, or an explicit
   admission `False`. Never revived by any other reading.
2. **`NOT-ACCEPTED · N of M bearers locally admitted — client path not
   accepted`** — meter-bound + locally admitted, with no client-path
   acceptance evidence (Conta B today). Never the unevidenced wording.
3. **`THROTTLED` / `PACING`** — pacing signals, named in the detail suffixes
   even when 1–2 win the headline.
4. **`UNKNOWN · capacity unknown — no auth/permission evidence …`** — no
   positive evidence at all (finding 11 preserved).
5. **`HEALTHY · all N bearers clear`** — positive acceptance evidence only.

## Unaccepted-client acceptance source (future)

Route C1's real-client acceptance will arrive through the sanitized map
(`client_paths=`), supplied by the trusted live side; until then absence is
explicitly "not accepted", never healthy and never unknown.
