# THRTL task-state/create plan — idempotent (03/10 17:0x BRT)

> **Correções canônicas (root 16:58):** (1) audit **T12 NUNCA vira Done por
> causa do #266** — #266 é só contrato/falsificadores; T001-T006 seguem
> desmarcados em `specs/245-prospective-admission/tasks.md`; T12 fica Backlog/
> em-progresso até features executáveis + acceptance de calibração passarem.
> (2) Manter **TRÊS pacotes reais**: THRTL17 (com checklist G1-G5 na
> description) + T12 + T13 — sem 8 filhos redundantes. (3) **THRTL17 fica
> started-equivalent** enquanto a implementação está ativa, não Backlog até G5.

Authority: Plane project **THRTL** = `c322c81e-1db2-4258-b6cb-81bcdbf22925`
(NOT generic FLEET). Parent umbrella: **THRTL-17** (Xiaomi) =
`cb97eb6d-27ae-467b-9214-9570944e115a`, Backlog, updated 02/10 19:22 UTC.
Board snapshot (Mac read-only): 17 items = 12 Done + 5 Backlog (THRTL-17/16/12/9/8;
16=source-pin stale Aug, 12=account-admin historic, 9=pricing historic,
8=subscription historic). No billing/visibility changes anywhere below.

**Numbering anti-confusion (canonical):** audit **T12 = prospective token
reservations (spec245 / proxy #266)** and audit **T13 = universal provider
registry**. These are NOT spec221's T12 (PR task, merged) or spec221's T13
(live-verification, blocked on closed Z.AI admission).

**Merged heads to record verbatim:** proxy #284 =
`f4612584fa05115acf5f2423ce38f2f74dfb80f2`; #285 =
`052b5410eb1d2f9cbe280bef7c3b6d53d8cde741`; #286 =
`676cfefa7e1cd88b41c2b552014edcd361efd435`; NixOS-2600 = `9461eedf` (head
`5fcc8942` green, merged 16:50:47).

## Idempotency contract (every op)

1. GET by exact-title search within THRTL; exact title match == identity key.
2. Compare (state, description) against desired; update ONLY deltas.
3. Absent → create with the exact title below; rerun is then a no-op.
4. Never delete, never touch billing/visibility/priority; parents only at create.
5. `updated_at` newer than 5 min with foreign description → skip + report
   (conflict signal, do not clobber concurrent edits).

## Rows (desired state)

| # | Exact title (identity key) | Desired state | Notes |
|---|---|---|---|
| U | THRTL-17 (existing) | **started-equivalent**; refresh description | checklist G1-G5 INLINE (não filhos) + heads merged; aberto até G5 |
| G1 | `G1 — validator accepts 2|3 mimo seat rows` | started-equivalent | pH active; fail-closed outside 2|3; fixtures only |
| G2 | `G2 — producer emits mimo:team-b conditionally` | started-equivalent | pH; default OFF; env+ASSIGNED gate; no seat aggregation |
| G3 | `G3 — UI renders third row without summing` | started-equivalent | pK; spec-285 classes per row + exhaustion acceptance |
| G4 | `G4 — seat-B live enablement` | Backlog | [pending] pF config + pJ rollout; depends G1-G3 |
| G5 | `G5 — real capacity acceptance` | Backlog | depends G4; real receipts only (PASS/FAIL+generatedAt+status+usedPercent); fixtures never prove live |
| T12 | `T12 — prospective token reservations (spec245 / proxy #266)` | **Backlog/em-progresso — NUNCA Done por #266** | contrato/falsificadores apenas; T001-T006 desmarcados; fechar só com features executáveis + calibração (T005) |
| T13 | `T13 — universal provider registry` | Backlog | registry reconciliation source for token-reservations/provider-registry rows; THRTL is authority |

As linhas G1-G5 acima viram CHECKLIST dentro da description do THRTL-17
(não itens separados): G1 validador 2|3 · G2 emissão condicionada default OFF ·
G3 UI sem somar + exaustão · G4 enablement ([pending] pF+pJ) · G5 acceptance
real (fixtures nunca provam live).

## CLI sequence (Mac, throttler-orch resolver)

```bash
S=scripts/plane-cli.js
node $S state --project THRTL                       # discover state names/UUIDs (backlog vs started-equivalent)
node $S issues --project THRTL --search "G1 — validator"    # repeat per title; exact match?
node $S create --project THRTL --parent cb97eb6d-27ae-467b-9214-9570944e115a --title "<exact>" --description "<desc>" 
node $S update --id <uuid> --state <started-uuid>   # only when state differs
```

- Parent link to THRTL-17 at create where supported; if the API rejects parent,
  create standalone and note the linkage in the description (no retries storm).
- Started-equivalent = whatever non-backlog "started" group the `state` call
  shows; if the board has no started state, leave Backlog and append
  `[in-progress]` to the title-independent description (title stays exact).
- T12 write is gated on the `gh api` verification output being recorded in the
  same run; if the check cannot run, leave state untouched and report.

## Remaining acceptance (reality check)

- G1-G3: in flight (pH/pK). G4-G5: not started; requires protected pF evidence
  + pJ rollout. **No pool-wide live claim exists or is permitted.**
- spec221 live-verification (NOT audit T13): still blocked — Z.AI replay under
  closed admission.
