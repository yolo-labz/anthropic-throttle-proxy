# Throttler static analysis — 07/10/2026

Generator family: **openai**. Assigned branch: `314-static-analysis`.
Source-only integration slice; **no merge or runtime activation claimed**.
Coordination contract: Throttler plan of 07/10/2026; canonical Sonar runbook and
`MS-RESEARCH-metrics-oracle-2026-10-06`. Raw receipts: [`static-analysis-314/`](static-analysis-314/).

## Exact-head baseline (not candidate acceptance)

Read-only Sonar API snapshot identifies analyzed revision
`3c64a535ea6902f00245173696d1005d5114eb0a`, analysis
`1a83ed53-44d3-4ffc-88dd-bf216d9eca95`. Main Actions scan
[37227676473](https://github.com/yolo-labz/anthropic-throttle-proxy/actions/runs/37227676473)
succeeded on that same SHA. The receipt includes its collection timestamp.

| Sonar metric | Measured value |
|---|---:|
| Maintainability / reliability / security | **A / A / A** (1.0 each) |
| Overall / new coverage | **93.5% / 93.5%** |
| Overall / new duplication | **0% / 0%** |
| Overall / new open issues | **0 / 0** |
| Bugs / vulnerabilities / code smells | **0 / 0 / 0** |
| Security hotspots | **0** (empty list) |
| Reviewed hotspot percentage | **absent / not applicable**, not a measured 100% |
| Quality gate | **OK**, ignoredConditions=false |
| Branch coverage | **absent**; existing collection is line-only |

These are **src-only** scores, not repository-wide perfection. Sonar indexed 38
files. New-code definition is inherited `PREVIOUS_VERSION`, project version
`not provided`, reference period 22/05/2026: its 7,717 new lines are not this
PR's diff. Overall lines to cover=8,260, uncovered=534; new uncovered=500.

The live built-in **Sonar way** gate requires new issues=0, new coverage≥80%,
new duplication≤3%, new hotspots reviewed≥100%. Ratings are read explicitly;
they are not separate live gate conditions. No threshold or instance setting
was changed. No hotspot was marked Safe/Fixed.

## Live inventory and authentication

Read-only SSH `ProxMox.Dokku`: `sudo -n dokku apps:list` enumerated **45 apps**.
Sonar is running, image `dokku/sonarqube:latest`; API confirms edition
**community**, version **26.3.0.120487**, exactly one branch named `main`.
Installed analyzer versions are recorded in `baseline-sonar.json` (Python 5.18,
JS/TS 11.8, Web 3.24, IaC 2.6.1, text/secrets 2.41, etc.).

| Surface | Role / current scope |
|---|---|
| Dokku `sonarqube` | Deterministic code/coverage/duplication/security engine |
| Dokku `languagetool` | Prose analyzer, deployed image `dokku/languagetool:latest`; API version/availability not measured; no repo prose gate found |
| Dokku `glm-critic` | Advisory content critic, not deterministic static analysis |
| Dokku `reactflux` | RSS frontend (`rss.home301server.com.br`, title ReactFlux), not an analyzer |
| Other 41 Dokku apps | Platform/application services; no additional static analyzer identified |
| GitHub CodeQL / OSV / Scorecard | Off-Dokku analysis; exact-SHA receipts and open alerts retained |
| Slop / alignment / Ruff / mypy | Host/Actions surfaces, not Dokku apps |

`rbw unlocked` succeeded. Credentials stayed inside subprocesses and SSH stdin;
no password/token is retained in these artifacts. Public Sonar requests returned
HTTP 403, including authentication validation. The **same stored admin password**
authenticated through Dokku's existing loopback nginx (SSH; no port/ACL changes).
Standard basic auth works there; the runbook's historical trailing-colon workaround
was not needed. This is not evidence that the public endpoint is healthy.

GitHub `SONAR_HOST_URL` exists; `SONAR_TOKEN` secret metadata exists (updated
20/09/2026). Its value/scope cannot be read through GitHub; the successful main
scan proves authentication at its run time, not indefinite future validity.
Ruleset—not classic branch protection—requires `code-slop + alignment`,
`ruff + pytest`, and `scan`; zero required approvals, resolved threads, PR/squash,
no deletion/force-push. No ruleset/permissions/secrets were changed.

## Real gaps repaired in this branch

1. **Community PR overwrite:** analysis history contains PR revisions
   `7cee7ebc72b2da8d2cc9827a89172ac83fb664bf` and
   `805037e674d37a90f01e990463fef249813e493e` in the one main project.
   PR run [37227370635](https://github.com/yolo-labz/anthropic-throttle-proxy/actions/runs/37227370635)
   logs Community Build and that PR's SCM revision, without a native PR branch.
   Keep required PR `scan` as report validation; publish only main push/main
   manual dispatch, explicitly stamping `sonar.scm.revision`. Non-main manual
   dispatch fails. Main Sonar is a **post-merge alarm**, not a PR Sonar gate.
2. **Silent missing-config green:** missing host/token now fails main publication;
   there is no successful skip fallback. PRs do not receive Sonar credentials.
   Workflow token permissions reduced to `contents: read`.
3. **Coverage/report blind spots:** scan roots expand to `src,scripts,clients,load,
   specs,.github,Dockerfile`, without new exclusions. Python collection adds scripts
   and specs, including unimported namespace directories. Checker rejects stale,
   missing, malformed, zero-hit, unmatched, incomplete, duplicate, DTD-bearing,
   inconsistent and dirty/wrong-revision reports. Source/report SHA-256 receipt
   and scanner task receipt are retained by the SHA-pinned artifact action.
   This verifies attribution/structure, not business-test adequacy or cryptographic
   attestation of a malicious runner. JS/TS coverage is still **unknown**.

## Checks and acceptance scope

- `ruff check src tests scripts/check-sonar-report.py`: **PASS**; format **PASS**.
- `actionlint .github/workflows/sonar.yml`: **PASS**.
- Planted missing/bad report tests: **25 passed**; CLI failures exit 1 and emit no
  receipt, including wrong revision and dirty checkout. DTD and NaN inputs fail.
- Checker mypy 2.1.0: **exit 0**. Existing report-only source mypy: **137 errors**
  across 15 modules, exit 1; retained verbatim, not suppressed or repaired here.
- Initial admitted full run: **2,072 passed**, 154 warnings, 110.28s. Its apparent
  91.51% Python coverage was **rejected** by the new inventory check: four
  unimported spec tools were missing. Native `include_namespace_packages=true`
  repairs collection; regenerated XML from the old dataset does not prove that
  repair. Actual first PR scan below proves complete namespace collection.
- Initial staged slop: score **100**, 0 errors/warnings; jscpd whole-snapshot
  0.2278%, 17 clones/128 lines; ratchet **0 relevant / 0 regressions**, exit 0.
  Alignment **0 errors/warnings**, exit 0. This is not a whole-repo zero-clone claim.
- That admitted job's cgroup budget was **4 CPUs / 4 GiB**, peak 260.5 MiB;
  serial tests, no thread multiplication. Subsequent heavy admission failed with
  `Unit desktop-job-slot.service was already loaded or has a fragment file`;
  read-only `systemctl show` returned inactive/dead, MainPID=0. No unit reset,
  cleanup or shell admission-wait loop was used. Fresh rerun goes to existing CI.

### First actual PR scan and protocol regression

PR [#315](https://github.com/yolo-labz/anthropic-throttle-proxy/pull/315), code head
`9da2aa8ba1040103fb5921394ed2dd84ae820151`: CI
[37674868147](https://github.com/yolo-labz/anthropic-throttle-proxy/actions/runs/37674868147)
ran **2,072 tests successfully**, collected **42 Python source files**, with
**8,083 / 9,091 covered lines = 88.9121%**. The native namespace option works.
The check correctly stayed red when report validation failed; no empty receipt
or green skip was manufactured. Raw XML is retained as `ci-first-coverage.xml.gz`
with its digest and failure details in `ci-first-coverage-summary.json`.

Root cause of that decoder failure: coverage.py 7.14's `xmlreport.rate` emits
**four significant digits** (`0.8891`), while the first checker compared it to
an unrounded fraction with an incorrectly tight tolerance. Compare the exact
producer-format value instead; tests cover valid `2/3 -> 0.6667` and forged
`0.6668`. This changes **no coverage threshold** and is backed by the actual
report/producer implementation, not symptom similarity. Fresh follow-up CI is
required for this corrected code; first-head CI is not final-head approval.
Slop/alignment, Ruff/full pytest, OSV and throwaway Docker build passed at the
first PR head; mypy job is green **report-only**, not type-clean.

## Findings outside this slice / precise follow-ups

- CodeQL open **#13** (`ratelimit.py:55`) and **#53** (`test_accounts.py:27`),
  `py/weak-sensitive-data-hashing`, on baseline SHA. #13 hashes bearer headers for
  anonymous correlation, not password storage; #53 is test setup. Keep both open
  until rule-specific review proves disposition; no global filter/dismissal added.
- Seven open Scorecard alerts: branch protection, code review, security policy,
  fuzzing, CII badge, two unpinned Dockerfile image findings. No claim that an
  external badge/score equals executable security acceptance.
- OSV exact baseline SHA has **0 results**. Workflow is report-only and uploads
  SARIF; absence of findings is not evidence of a blocking vulnerability gate.
- Mypy legacy debt: proxy 59; limiter 25; UI routes 18; other modules 35.
  UI/limiter/runtime fixes belong to their owners/follow-up slices.
- Existing Sonar S7503 package-wide suppression and Web S6845 template-wide
  suppression are broader than their documented aiohttp/scroll-region rationale.
  Existing CodeQL `py/partial-ssrf` is a global rule filter with a fixed-upstream
  rationale/reversal condition. Re-audit actual callers before narrowing/removing;
  they can hide unrelated future violations. Suppression inventory is retained.
- Ruff currently gates src/tests plus this new checker, not every legacy tool;
  remaining scripts/client harness lint/type/coverage need their own slices.
  No exclusions or suppressions were added to make these gaps disappear.
- Sonar navigation reports version EOL **08/06/2026**. Assess a separately
  authorized server upgrade; no instance deployment/change in this slice.
- `throttler-sonar-gate-green` ledger diagnosis is stale (implementing since
  24/09/2026 despite measured OK). Verdict attempt recorded with actual SHA/gate;
  its public-URL oracle still needs transport repair. No false verified status.

## Delivery boundary / next action

Ordinary PR with Actions changes: **operator-authorized merge required**. No
self-merge, production deploy, live-unit changes, model-selection changes or
shared-vault edits. After authorized merge, read main scan's retained task/source
receipts and Sonar API analysis revision; measure the **expanded scope** gate,
ratings, issue/hotspot lists, coverage and duplication. Until then candidate
Community Sonar metrics and runtime activation remain **unknown/unperformed**.
Reversal after merge: one ordinary PR reverting its squash commit.
