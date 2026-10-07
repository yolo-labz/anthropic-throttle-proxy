# Throttler: ranked improvement roadmap — 07/10/2026

## Scope and evidence boundary

Research-only, generator family **openai**, branch `316-improvement-research`.
Repository baseline: `3c64a535ea6902f00245173696d1005d5114eb0a`.
Date verified on desktop with `date`: **07/10/2026, 16:13 BRT**.
The frozen coordinator plan was read; shared plan/save-state, source code,
credentials, runtime, quotas and model selection were not changed.

This is an options roadmap, not the parallel gap audit. The integration,
gauge and static-analysis workers own their current repairs. Items below are
follow-on slices: dispatch only after checking their landed artifacts for overlap.
No incident, live quality score or activated improvement is claimed here.

Evidence labels:

- **Verified source/check:** inspected repository or public upstream code, or a
  reproducible synthetic check. This does not establish deployed behavior.
- **Vendor/standard statement:** primary documentation; vendor compatibility
  statements are not acceptance evidence for a different provider.
- **Hypothesis:** proposed benefit or failure mechanism requiring the stated check.

Effort estimates are engineering time for the smallest slice, not delivery promises.
Ranking favors reduced maintenance/duplicate spending and trustworthy evidence,
not more throughput or an invented aggregate quality score.

## Ranked shortlist

| Rank | Improvement | Benefit | Smallest effort | Risk | Prerequisite |
|---|---|---|---|---|---|
| 1 | Qualify and contain the existing Desktop session adapter | High: bounds an unsupported authentication seam | 1–2 days | Medium; patched bridge may differ | Integration source artifact |
| 2 | Reuse exact-native transport rehearsals as upgrade qualification | High: detects provider/SDK semantic drift before publication | 0.5–1 day | Low for receipt-only slice | Known candidate package paths |
| 3 | One replay-safety contract and remaining deadline across retry layers | High: avoids uncertain duplicate POSTs and stacked waits | 1–2 days for classification tests | High for later behavior changes | Rank 2; owner agreement |
| 4 | Bounded incremental usage capture, with explicit completeness | High: late usage survives without retaining full responses | 1–2 days | Medium; usage vocabularies differ | Gauge worker's landed parser contract |
| 5 | Bound and version local observation reports using existing patterns | Medium: predictable parsing and extensible provenance | 0.5–1 day for reader bounds | Low–medium; producer compatibility | Producer sample sizes/schema |
| 6 | Consume exact-analysis quality receipts, not live render-time APIs | High: connects existing analyzers without a new scoring engine | 1 day for local reader/tests | Low for optional read-only panel | Static-analysis baseline; rank 5 |
| 7 | Do not build another router, OAuth broker or dashboard framework | High avoided maintenance; preserves proven paths | 0.5 day for decision/acceptance checklist | Low | Existing worker acceptance |

## 1. Qualify and contain the existing Desktop session adapter

**Present evidence / already-present check.** The proxy has no Desktop SSO
implementation. The public bridge [S2] already has session caching, invalidation,
thread locks and an in-flight map; upstream `tests/test_desktop_session.py`
contains a cookie-database path test, not session contention/cancellation tests.
Its `async get_service_cookie()` calls synchronous `threading.Event.wait(timeout=60)`
when another refresh is in flight. That is a verified upstream source property,
not a measured stall in the installed, patched SGP bridge. The Nix source comment
at `modules/home/pi.nix:2293–2294` describes SGP/`sid=mimosgp` patches; their
actual packaged implementation was not inspected in this slice.

**Supported alternative and limit.** Xiaomi says Desktop membership benefits
are available within Desktop, while Token Plan is a separate subscription with
its own API key and supported external tools [S1]. Therefore:

- Reusing the official API-compatible Token Plan is a supported external-tool
  alternative **for that separate entitlement**, not a replacement for paid Desktop usage.
- Using the official Desktop application is the documented Desktop-membership path.
- The session bridge is a third-party reconstructed SSO adapter, not a documented
  Xiaomi Desktop authentication API. Do not invent an OAuth refresh-token service
  or silently switch it to Token Plan/pay-go. No vendor endorsement is established.

**Hypothesis / falsifier.** Refresh contention can block an async bridge and
make healthy quota look unavailable. Falsified for the deployed candidate if its
SGP patches already implement non-blocking, bounded coalescing.

**Smallest PR slice.** In the bridge/Nix owner's branch, pin the exact upstream
revision plus patch identity; add synthetic refresh-contention and error-sanitization
checks before changing authentication code. If the problem survives the patch
comparison, replace only the blocking async wait with native awaitable single-flight
coordination. Keep credentials, cookie discovery and regional endpoint patches intact.

**Measurable acceptance.** Two concurrent callers produce one mocked SSO flow;
a control coroutine progresses while the second caller waits; cancelling one waiter
does not cancel the shared refresh; a stalled refresh terminates within an explicit
configured bound. Auth errors expose only bounded categories, never response bodies,
SSO URLs, cookies or seed values. No real account call is necessary for these tests.
Coordinator separately owns real Desktop chat, quota attribution and activation.

**Rollback / sequence.** Revert the bridge-test/coordination PR and restore its
previous package pin through the normal coordinator flow; preserve the distinct
Desktop and Token Plan lane identities. Do first, after integration lands.

## 2. Reuse exact-native transport rehearsals as upgrade qualification

**Present evidence / already-present check.**
`clients/transport-fetch-hold/{rehearsal,mimo-rehearsal}.mjs` already exercise the
installed native Anthropic and OpenAI-compatible provider paths, cancellation,
byte-exact SSE and explicit retry configurations. `clients/pi-queue-wait/README.md`
already documents strict provenance/body/zero-usage checks and separate loopback
provider tuples. The accepted source explicitly distinguishes configured fixtures
from runtime; it is not an installation or fleet-wide acceptance claim.

**Primary-source support.** OpenAI's generated stream contract explicitly says
an interrupted stream may omit its final usage chunk [S5]. HTTP semantics also
make retry safety dependent on request processing, not merely a failed connection [S3].
These are reasons to qualify the real provider seam, not add a second SDK to the proxy.

**Hypothesis / falsifier.** An upgrade can preserve the exported API while changing
native retry/event semantics. A receipt demonstrating unchanged behavior for the
exact candidate paths/hashes falsifies that concern for that candidate only.

**Smallest PR slice.** Extend existing rehearsal output with resolved provider/SDK
versions, module content hashes, fixture revision and effective retry options.
Retain the existing positive and negative controls; do not create another harness,
a new provider adapter or a new inference service. Use a repository-owned receipt.

**Measurable acceptance.** Both existing native-path rehearsals pass against the
candidate package using synthetic loopback endpoints. Their failure controls exit
nonzero. The receipt identifies actual loaded modules; moving to another package
invalidates that receipt. Explicit `maxRetries=0` uncertainty cases make exactly one
server-accepted send; configured positive retries remain separately labeled.
No remote model inference, runtime census or automatic activation is implied.

**Rollback / sequence.** Revert the receipt-only change; existing rehearsals still
run unchanged. Make this the prerequisite for rank 3 and later transport upgrades.

## 3. One replay-safety contract and remaining deadline across retry layers

**Present evidence / already-present check.** Queue budgets are already inherited
through `_effective_queue_max_wait` (`proxy.py:831`). The client queue wrapper already
limits rejections/wait and adds positive jitter. Committed streams already propagate
`StreamCommittedError` (`forwarding.py:297`, `proxy.py::_try_forward`); the source
must not retry after response commitment. Keepalive holds already have bounded-wait
regressions in `tests/test_keepalive_hold.py`. Do not rebuild admission/AIMD or add
a generic backoff dependency.

However, the ordinary fallback path at `proxy.py:2180–2256` still selects another
attempt after an upstream error. Its existence does not establish whether every
error class proves non-processing. A silent/pre-header failure is not equivalent
to the proxy's proven pre-egress queue refusal.

**Standard statement.** RFC 9110 §9.2.2 says a proxy MUST NOT automatically retry
non-idempotent requests; its client exception requires knowledge of idempotence or
non-application [S3]. A `Retry-After` value alone is not such proof. Provider-specific
replay policy therefore needs explicit evidence; this roadmap does not certify the
current proxy as conformant. SSE/EventSource reconnect semantics [S4] do not grant
resumption/idempotency to a generation POST.

**Hypothesis / falsifier.** Independently bounded layers can still exceed caller
patience or replay an uncertain send. Falsify with a cross-layer trace proving one
remaining deadline and no extra send after ambiguous acceptance, not a happy-path 200.

**Smallest PR slice.** Add a deterministic fake-clock/request-count test matrix
around existing helpers: pre-egress refusal; connect failure with positive unsent
proof; server accepts then closes before headers; raw upstream 429/503; committed
SSE disconnect; cancellation. Record which layer owns each retry and where the
remaining budget ends. Then make the smallest shared-policy correction supported
by that matrix, with unknown processing failing closed. Do not insert a retrying
SDK/tenacity layer or change global fleet retry configuration.

**Measurable acceptance.** With an explicitly configured short admission/retry
budget, no new physical attempt starts after its deadline; a long `Retry-After`
returns advice rather than being shortened into an early retry. Ambiguous acceptance
and committed SSE have one physical send and zero replay. Proven local queue
refusals may retry only inside the existing bounded client contract. Count-based
acceptance, fake clocks and cancellation checks are sufficient; no load test needed.
The deadline bounds admission/retry, not the duration of a legitimately admitted stream.

**Rollback / sequence.** Revert the narrowly scoped classification/policy PR;
retain the tests/evidence for the coordinator. Behavior changes need rank 2 and
explicit regression coverage for central/direct fallback and each generation family.

## 4. Bounded incremental usage capture, with explicit completeness

**Present evidence / already-present check.** Both `_stream_response`
(`forwarding.py:297–374`) and `_pipe_sse_upstream` (`proxy.py:2736–2745`) retain
the **first** 1 MiB, while `_record_usage` parses the captured bytes after forwarding.
`ratelimit.py::_parse_sse_usage` already normalizes OpenAI cached-token conservation;
`tests/test_tps_gauge.py` covers this. Reuse that arithmetic and the existing history
ring. Do not duplicate the gauge worker's current conservation repair.

**Verified synthetic result.** A 1,048,698-byte buffer with a late usage event yields
`input=5, cache_read=9, output=24` when parsed whole; its first 1,048,576 bytes yield
all zero. This proves a prefix-capture blind spot, not its incidence in live traffic.
OpenAI documents final usage before `[DONE]` and possible omission on interruption [S5];
this is not proof that every MiMo model emits that contract.

**Smallest PR slice.** First add the late-usage regression at the actual forwarding
seam. Then feed bounded SSE event fragments to the existing usage normalization,
retaining only necessary start/final usage metadata. A tail ring alone would lose
Anthropic start usage; increasing the whole-body cap only moves the failure boundary.
Use stdlib incremental decoding/JSON and the SSE framing rules [S4], not an SDK.

**Measurable acceptance.** A stream exceeding the current prefix cap delivers the
same bytes to the client and preserves late usage; arbitrary chunk/UTF-8 boundaries,
CR/LF framing, comments, nested cached fields and interruption are covered. Keep
memory bounded by an explicit event-buffer cap, not response length; over-limit or
missing terminal usage is labeled incomplete/unknown rather than zero or estimated
spend. Protocol-specific cumulative-versus-delta handling must be checked against
each provider contract. No assumption that successful HTTP means complete billing.

**Rollback / sequence.** Revert parser/capture integration; keep the reproducer.
Coordinate after the gauge fix and rank 2 so its existing normalization tests survive.

## 5. Bound and version local observation reports using existing patterns

**Present evidence / already-present check.** `lanes.py:460–552` already distinguishes
sample cadence from HTML polling and degrades invalid/future/stale samples;
`tests/test_dashboard_data_contract.py` protects these semantics. But `_read` uses
unbounded `Path.read_text`/`json.loads`, without a schema version or duplicate-field
rejection. `provider_registry.py` already demonstrates bounded descriptor reads,
regular-file validation, duplicate rejection and atomic publication ownership.
`ledger.py` already has versioned snapshots and same-directory atomic writes.

**Primary-source statement.** Python's JSON documentation recommends limiting
untrusted JSON size and documents accepted duplicate names and non-finite values [S6].
This supports reuse of the existing pattern, not introducing Pydantic/JSON Schema.

**Smallest PR slice.** Apply a measured read cap and regular-file check in the lane
reader; keep its existing fail-closed presentation and five-second cache. Do not
import a private registry helper merely to couple unrelated schemas. A second,
producer-coordinated slice can introduce `schemaVersion`, per-source observation time,
producer version and provenance fields with an explicit legacy-read migration window.
Choose the cap from representative sanitized producer fixtures, not guessed capacity.

**Measurable acceptance.** Existing lane fixtures remain valid; oversized, FIFO,
duplicate-field, non-finite, truncated, future and unsupported-version fixtures
cannot certify healthy capacity or block the event loop on a FIFO read. Fresh legacy
reports retain their documented behavior during migration. A producer failure keeps
its last observation timestamp/error visible; it never restamps old data as new.
No per-account seed, key, email or raw analyzer response is added to public reports.

**Rollback / sequence.** Reader-bound PR reverts independently. Versioned-reader
rollback is safe only while the writer keeps the agreed legacy-compatible fields;
retain that compatibility through the rollout. Do before adding report consumers.

## 6. Consume exact-analysis quality receipts, not live render-time APIs

**Present evidence / already-present check.** `.github/workflows/sonar.yml` already
runs coverage and uses `sonar.qualitygate.wait=true`; Scorecard/OSV workflows already
publish SARIF. These are existing analyzers, not invitations to add another scanner.
`ui/routes.py` has background collection/local snapshot patterns and server-rendered
partials; `lanes.py` is the local-report precedent. Existing rendered views are not
proof of quality-report integration. The static-analysis worker owns live inventory,
metric baseline, edition/capability verification and any workflow repair.

**Primary-source support.** Sonar's `ce/task` endpoint requires an analysis task ID
and permissions; `qualitygates/project_status` accepts `analysisId` and returns
OK/WARN/ERROR/NONE, with missing analysis distinguishable [S7]. Querying only a
project's current status can associate another analysis with the requested commit.
Upstream endpoint source is evidence of its API, not the installed server version.

**Smallest PR slice.** Consume the static-analysis worker's accepted sanitized receipt
through a bounded local reader and optional Jinja partial. If no compatible receipt
exists, define it with that owner first: tool/version, repository SHA, source/ref,
task/analysis IDs, actual gate status, observation time and missing/error state.
An out-of-process producer resolves task completion → analysis ID → gate and supplies
available issue/hotspot/coverage/duplication summaries. Keep auth in that subprocess;
do not fetch Sonar/GitHub/Dokku or raw private findings during HTML rendering.

**Measurable acceptance.** Exact-analysis match renders the real gate and metric
values/units; wrong SHA, pending/failed task, absent metric, NONE gate and expired
receipt visibly remain unmatched/pending/unknown—not green or 100%. Render tests pass
with outbound networking unavailable. UI visibility never changes actual required
checks, thresholds, exclusions, hotspot review state or scanner findings. If the
installation cannot analyze the requested ref, display that limitation rather than
presenting a main-project status as a PR gate. No public badge/privacy-policy change.

**Rollback / sequence.** Remove the optional reader/panel in one revert; existing
CI gates/analyzers remain untouched. After the static-analysis artifact and rank 5;
coordinate UI ownership rather than duplicating its work in this branch.

## 7. Do not build another router, OAuth broker or dashboard framework

**Present evidence / already-present check.** The current stack already supplies
fair scheduling, queue drain estimates, authoritative admission, subscription
constraints, prospective reservation lifecycle, native-client hold seams,
server-rendered Jinja/HTMX and bounded history. `pyproject.toml` contains no vendor
AI SDK; `docs/ARCHITECTURE.md` describes the existing two-tier topology. Some prose
still mentions a removed GROQ advisor; that is documentation debt, not a feature
request. Vendor Desktop/API entitlement separation [S1] provides no documented
basis for a new universal Desktop OAuth broker.

**Decision.** Reuse these pieces. Reject a Redis-backed queue, service mesh,
SPA/charting rewrite, generic AI quality scorer or additional provider SDK unless
a measured requirement defeats the existing primitive. Do not revive the advisor
to summarize scanner scores. This avoids a new failure domain without sacrificing
a requested capability; ranks 1–6 are the concrete alternatives.

**Measurable acceptance / smallest slice.** A follow-up documentation PR maps each
accepted improvement to its existing helper/owner and corrects stale advisor prose
without changing the no-SDK invariant. Every implementation PR names its native seam,
leaves render collection network-free for new panels, and adds no dependency unless
its acceptance test demonstrates necessity. This research PR is the initial decision
record; no infrastructure or runtime change accompanies it.

**Rollback / sequence.** Revert the documentation decision if a concrete requirement
wins. Revisit only with an executable counterexample, not a framework preference.

## Primary sources and retrieval provenance

All sources below were fetched directly with HTTP 200 on **07/10/2026**.
Undated/live pages are dated by retrieval only; they are not assigned a publication date.

- **[S1] Xiaomi, Token Plan and Desktop Membership**, publication date unavailable;
  [official FAQ](https://mimo.mi.com/docs/en-US/quick-start/faq/token-plan/desktop-guide).
  Also fetched [Desktop integration](https://mimo.mi.com/docs/tokenplan/integration/mimo-desktop):
  API and Token Plan keys are not interchangeable. These are vendor statements.
- **[S2] Bridge maintainer source**, commit `b42443f004de0da84ea1e76ac82df84da1b2b4a3`,
  **18/09/2026 (UTC)**:
  [session adapter](https://github.com/Fly143/xiaomi-mimo-desktop-api/blob/b42443f004de0da84ea1e76ac82df84da1b2b4a3/app/desktop_session.py),
  [existing test](https://github.com/Fly143/xiaomi-mimo-desktop-api/blob/b42443f004de0da84ea1e76ac82df84da1b2b4a3/tests/test_desktop_session.py).
  Primary for the third-party implementation, not a Xiaomi-supported protocol.
- **[S3] IETF RFC 9110**, **06/2022 (publication month)**:
  [§9.2.2 idempotent methods](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2),
  [§10.2.3 Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3).
- **[S4] WHATWG HTML Living Standard**, live/undated:
  [SSE parsing and reconnect](https://html.spec.whatwg.org/multipage/server-sent-events.html).
  Framing guidance is reusable; browser EventSource reconnect is not POST replay safety.
- **[S5] OpenAI generated stream options**, commit
  `4e152cdefe1844c2d5d78653310e9b9c0195c44e`, **06/10/2026 (UTC)**:
  [include_usage contract](https://github.com/openai/openai-python/blob/4e152cdefe1844c2d5d78653310e9b9c0195c44e/src/openai/types/chat/chat_completion_stream_options_param.py).
  Vendor contract for OpenAI, not an assertion about MiMo's backend.
- **[S6] Python JSON documentation**, live/undated:
  [size warning and decoder behavior](https://docs.python.org/3/library/json.html).
- **[S7] SonarSource endpoint implementations**, commit
  `4c9486b55e1a39e02609e8ee1636863947cf54a4`, **06/10/2026 (UTC)**:
  [CE task](https://github.com/SonarSource/sonarqube/blob/4c9486b55e1a39e02609e8ee1636863947cf54a4/server/sonar-webserver-webapi/src/main/java/org/sonar/server/ce/ws/TaskAction.java),
  [quality-gate status](https://github.com/SonarSource/sonarqube/blob/4c9486b55e1a39e02609e8ee1636863947cf54a4/server/sonar-webserver-webapi/src/main/java/org/sonar/server/qualitygate/ws/ProjectStatusAction.java).
- **[S8] aiohttp maintainer documentation**, commit
  `548154072f08514d654bb926166a16cf22eedb71`, **07/10/2026 (UTC)**:
  [client session/pooling/timeouts](https://github.com/aio-libs/aiohttp/blob/548154072f08514d654bb926166a16cf22eedb71/docs/client_quickstart.rst).
  Native session/timeouts already cover these primitives; no extra HTTP framework
  is warranted. Repository lock has aiohttp 3.13.0; this upstream document is not
  a claim that every current-main feature exists in that installed version.

### Search coverage and unavailable sources

Z.AI search and reader returned **1308 / five-hour usage limit**; no model delegation
or metered fallback was used. SearXNG served three independent public query angles:

1. `Xiaomi MiMo official Token Plan API desktop subscription session API`
2. `RFC 9110 automatic retry non idempotent POST SSE interrupted stream`
3. `SonarQube Web API quality gate analysisId ce task official`

Additional API-streaming and Sonar capability queries refined candidates. Some
SearXNG engines returned HTTP/CAPTCHA errors, but usable results were returned;
no search snippet is used as an authoritative answer. Official Sonar/aiohttp HTML
pages returned 403, so corresponding maintainer source was fetched instead.
Prometheus HTML/source attempts failed (403/404); no recommendation relies on them.
Transcript `recall` timed out; no missing memory was invented or treated as evidence.

## Executed receipts and remaining uncertainty

Baseline source checks, no inference/provider traffic:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src" \
  /home/notroot/Documents/Code/yolo-labz/anthropic-throttle-proxy/.venv/bin/python \
  -B -m pytest -q -p no:cacheprovider \
  tests/test_dashboard_data_contract.py tests/test_lanes.py \
  tests/test_provider_registry.py tests/test_tps_gauge.py
# 88 passed in 0.56s; exit 0; admitted resourceTier=heavy.
```

A source-only prefix reproduction also passed (1 MiB bound, no load test):

```python
from anthropic_throttle_proxy.ratelimit import _parse_sse_usage
cap = 1024 * 1024
late = (b'data: {"usage":{"prompt_tokens":14,"prompt_tokens_details":'
        b'{"cached_tokens":9},"completion_tokens":24}}\n\ndata: [DONE]\n\n')
wire = b':' + b'x' * cap + b'\n\n' + late
assert len(wire) == 1048698
assert _parse_sse_usage(wire) == {
    "input": 5, "output": 24, "cache_read": 9, "cache_creation": 0}
assert _parse_sse_usage(wire[:cap]) == {
    "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
```

Document validation passed: seven numbered options, eight primary sources, three
named query angles, per-option acceptance/slice/rollback checks, and the executable
Python block above. The commit hooks passed code-slop/alignment with **zero changed
code files** in alignment; those results are not a full-source quality verdict.

Native transport rehearsals, full pytest, Sonar and security scans were **not run
locally** in this research slice; CI status must be read at the final PR head, not
inferred from 88 tests. No independent model approval is claimed. Unknowns: actual patched bridge contention,
provider usage-completeness guarantees, analyzer edition/plugins and exact live
analysis identity, deployment incidence of the capture limit, final sibling diffs.

**Dispatch sequence:** rank 1 after integration; rank 2 before transport changes;
rank 3 after qualification; rank 4 after gauge normalization stabilizes;
rank 5 before rank 6; rank 7 is the standing reuse decision. Separate PRs and
owner-specific branches; runtime publication/activation remains coordinator-only.
No research blocker prevents accepting this roadmap. Implementation blockers are
named per item and are not disguised as verified defects or completed repairs.
