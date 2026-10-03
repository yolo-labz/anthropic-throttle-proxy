# Seat-B meter wiring + real capacity acceptance plan (03/10 13:45 BRT)

Status de entrada (root receipts): PR #285/pK MERGED `052b5410…` (router
acceptance); PR #286/pH b4d99f3 → updated `3dd16d9d…` (CI running, root
custody; coauthor corrigido para MiMo). A entrega do pH é **fundação do meter
B** — `main()` ainda chama `team_report` SEM `seat_b_response`. Sem claims de
B live ou pool-wide a partir de fixtures.

## Fronteiras (imutáveis)

- **Public code (pH):** probe/report/lanes/UI + testes sintéticos.
- **Protegido (pF, Astra):** identidade, project ids, credenciais, evidência crua de quota.
- **Consumidor Nix (coordenação pJ):** `mimo-plan-report.jq` + checagem de linhas do `mimo-dashboard.nix`.
- Terceira linha **OFF** até os validadores downstream aceitarem.

## Ordem de wiring (gates estritos)

1. **G1 — validadores aceitam 3 linhas** (testes Nix + fixtures): regra vira
   "exatamente 2 linhas sem Team B configurado; exatamente 3 com; fail-closed
   caso contrário".
2. **G2 — producer emite `mimo:team-b`** apenas com env `MIMO_TEAM_B_PROJECT_ID`
   definido E resposta válida+ASSIGNED; semântica fail-closed idêntica à linha
   team; nunca agregar assentos.
3. **G3 — UI renderiza a terceira linha** sem somar (classes spec 285 por linha).
4. **G4 — enablement live:** config pF + rollout pJ; receipt real pela lane protegida.
5. **G5 — aceitação de capacidade real** (abaixo).

## Aceitação de capacidade real (nada de claim por fixture)

- Amostra fresca com linha B: contadores medidos via receipt protegido (pF) —
  apenas PASS/FAIL + generatedAt + status + usedPercent.
- Janelas de tráfego limitadas via métricas existentes do proxy
  (served/inflight/queue/499); sem aumento de capacidade.
- Exclusão fim-a-fim (semântica pK, já merged #285): A esgotado/encolhido não
  atrai carga enquanto B saudável — verificado por contadores reais em janela limitada.
- **"Pool-wide live" só com G4+G5 + receipts reais; fixtures nunca bastam.**

## Handoff pendente (janela do shell)

- pH: implementar G1 (regra de validação com 2|3 linhas) + G2 (emissão
  condicionada, OFF por default) + testes sintéticos; SEM config privada, SEM
  coleta live; novo SHA publicado via PR normal.
- pF: mantém lado protegido; nenhum dado cru em pacote público.
