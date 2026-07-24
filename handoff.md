# Handoff: throttle incidents

This handoff is for the next Claude Code session touching this repository.
Read it before changing throttle behavior, central deployment, Nix pins, or
host activation. Latest incident first.

---

## 23-24/07/2026 - Spec 093 unified `:8760` ingress — proxy-side COMPLETE (S1–S6 merged), S7 = Nix deploy (Pedro-gated)

The "never run out of AI" router. **Proxy-side done: 6 merged PRs (#132–#137).**
The `:8760` ingress is a complete, opt-in, gated router across the three lanes.
S7 (Nix wire-through + fleet flip) remains — Pedro-gated (blast radius = every
claude-code tab; the Nix tab stated ":8760 stays on its schedule").

### What shipped (all in `src/anthropic_throttle_proxy/{ingress,routing}.py`)
- **S1 #132** — `:8760` ingress skeleton; forwards to a default lane byte-identical; opt-in, no-op-when-unset.
- **S2 #133** — role inference (generate/judge/bulk) from the model; bounded body read.
- **S3 #134** — gauge-driven lane selection; walks the role's chain (anthropic→kimi→glm; bulk never anthropic); polls each lane's `/__throttle/health`; auto-advances on a lock.
- **S4 #135** — model-remap on egress (`claude-*`→`kimi-k2.6`/`glm-5.2`); session stickiness (`metadata.user_id`); **root fix: strip `Content-Length`** (remap changes body length → stale CL hung the lane).
- **S5 #136** — `GENERATE_OVERFLOW_ENABLED` (default false = pre-kimi-k3 GA): generate is Anthropic-only, HOLDs (`503 ingress-generate-held`) rather than silently downgrading to kimi/GLM as Opus. Bulk/judge keep full chains.
- **S6 #137** — `ingress_route_decisions_total{role,lane}` counter + `GET /metrics`.

Verified models (23/07 live probes): Kimi accepts `kimi-k2.6`; GLM accepts `glm-5.2`; `kimi-k3` (generate overflow) GA 27/07.

### Gate calibration (IMPORTANT)
Cross-family gate ran on **GROQ `openai/gpt-oss-120b`** (codex quota-dead till 28/07, Anthropic lane degraded). GROQ is weaker than codex/claude: it **missed a real CodeQL-high ReDoS** (`\s*` regex anchors on S2) that **CodeQL caught** — fixed. It also issued a false-read BLOCKER on S4 (claimed the `content-length` filter is case-sensitive; it does `.lower()`). **S1–S6 flagged for the mandatory codex re-review when it recovers (28/07).** Lean on CodeQL, not GROQ, for the bar.

### S7 — Nix deploy (Pedro-gated, coordinate with the Nix tab `w1W:p2`)
1. Bump the proxy package pin `~/NixOS/pkgs/anthropic-throttle-proxy/default.nix:360` `rev` from `a58b6c7…` → the S6 head `fd84331…` (+ recompute the fixed-output hash via `nix build`).
2. Add a `unified-throttle-ingress` HM module on `:8760` pointing at the three lanes (`INGRESS_ANTHROPIC_LANE_URL=:8765`, `:8766`, `:8767`; `INGRESS_GENERATE_OVERFLOW=false` until 27/07+kimi-k3 verified). Reuses the `throttle-proxy-instance` factory pattern.
3. **Fleet flip (Pedro's moment):** canary ONE claude-code tab's `ANTHROPIC_BASE_URL=http://127.0.0.1:8760` first; confirm bulk→Kimi/glm + generate→Anthropic via the `x-anthropic-throttle-{role,lane}` response headers; then roll to the fleet. Activation is niri-guarded (`nh os switch`).

### Known model-id drift (S7 reconciliation)
NixOS #1330 / Issue #1281: the deployed subagent slot is `claude-opus-4-8[1m]` (same as primary-generate), NOT `claude-sonnet-4-6` — so model-tier inference maps subagent fan-out to `generate`, not `bulk`. Reconcile in S7 (re-pin the subagent model OR add a role header) so bulk fan-out hits the cheap lanes.

Reversal: one `git revert` per slice; the ingress is opt-in (no behavior change until an operator starts `:8760` + points a tab at it).

---

## 23/07/2026 - Spec 093 unified `:8760` ingress — S1 + S2 merged (build in progress)

The "never run out of AI" router (Spec 093, `specs/093-predictive-glide-spillover/`).
Pedro authorized starting ahead of the 28/07 gate (kimi-k3 GA 27/07 + codex
recovery 28/07): the router logic doesn't need either — pre-kimi-k3, invariant
6 is HOLD+flag (S5), and the cross-family gate used the GROQ fallback (#130
precedent) since codex is quota-dead and the Anthropic lane is degraded (2/3
accounts hard-capped). The Nix tab (`w1W:p2`) owns the **interim manual
switching** question (NixOS repo); S1–S6 are proxy-only, S7 is the Nix
wire-through (coordinate with the Nix tab there).

### Lanes verified live (23/07, the prerequisites)
- Anthropic `:8765` — `central_status=up`, 1/3 accounts open, 2 hard-capped.
- GLM `:8766` — client-provides-key, path `/api/anthropic`, healthy.
- Kimi `:8767` — concurrency-6, healthy.
- NixOS prereqs all merged: Kimi (#1326/#1328/#1329/#1333), z.ai (#1084),
  sock-read knobs (#1327 = the proxy #130 follow-up), routing canon #647
  rendered (#1330).

### S1 — ingress skeleton + no-op-when-unset (#132, merged)
New `ingress.py` — a separate-process aiohttp server on `:8760` that forwards
to a default lane (`:8765`) path-preservingly, client-visible-response
byte-identical. Opt-in (`INGRESS_HOST:INGRESS_PORT`); unset = clients hit
`:8765` unchanged (invariant 5). Local `/` probe, <50ms `/__throttle/health`,
hop-by-hop stripped both directions, `ClientError`→generic 503 (no detail
leak), `x-anthropic-throttle-ingress: 1` marker. Entry point
`anthropic-throttle-ingress`. GROQ gate ALLOW (no BLOCKER); self-repaired the
503-leak [MAJOR].

### S2 — role inference from the request model (#133, merged)
New `routing.py::infer_role(model)` → `generate`/`judge`/`bulk` (fable/opus →
generate; sonnet-5 → judge; sonnet-4-6/haiku → bulk). `ingress._forward`
buffers `POST /v1/messages` only (bounded read `ROLE_BODY_READ_LIMIT` 64 KiB —
larger bodies default role + stream through byte-complete) and stamps
`x-anthropic-throttle-role` on the response. **No routing change yet** (S3
selects the lane). Known drift documented: deployed subagent slot is
`claude-opus-4-8[1m]` (Issue #1281), not `claude-sonnet-4-6`, so model-tier
inference maps both opus uses to `generate` — S7 reconciles.

### Gate calibration — IMPORTANT for the remaining slices
The GROQ `openai/gpt-oss-120b` cross-family gate (round 2, ALLOW) **missed a
real ReDoS** (`\s*\[\d+[mk]\]\s*` on user-controlled model string) that
**CodeQL caught as high-severity**. Fixed (`\s*` anchors dropped — redundant
with the surrounding `.strip()`). GROQ is a weaker reviewer than codex/claude;
the #130 precedent was a default-no-op knob. **S3–S5 (lane selection +
never-hard-fail guards) are higher-stakes** — lean on CodeQL + rigor, and
flag S1–S2 for the mandatory codex re-review when it recovers (28/07).

### Remaining slices
- **S3** gauge-driven lane selection (the ingress polls each lane's
  `/__throttle/health` + unified gauges; walks the role's chain; picks the
  first open+under-threshold lane). — next
- **S4** model-remap on egress (`claude-*` → lane's id) + session stickiness.
- **S5** never-hard-fail + don't-silently-downgrade (HOLD+flag pre-kimi-k3).
- **S6** observability (per role→lane counters, Kimi low-balance gauge).
- **S7** Nix wire-through (`unified-throttle-ingress` HM module; flip the
  fleet's `ANTHROPIC_BASE_URL` to `:8760`; coordinate with the Nix tab).

Reversal: one `git revert` per slice (all default-no-op until an operator
starts the `:8760` process + points a tab at it).

---

## 22/07/2026 - Soft utilization glide + display-only usage-lock evidence (PR #129)

### Corrected diagnosis

The 1,004-second B window observed after the 17:43:16 dispatch was created by
the proxy itself after a successful upstream 200 reported utilization 0.90.
`_maybe_pause_at_target()` used the same `THROTTLE_UTILIZATION_TARGET=0.9`
value as reset-aware routing and `_maybe_glide()`, but called
`limiter.note_retry_after(reset-now)`. A soft preservation target therefore
became a hard until-reset admission lock. This was not evidence that Anthropic
had exhausted the account; A/C multi-day windows with the same log shape may
have had the same local-policy cause.

A second race amplified any hard Retry-After, regardless of source. A request
could acquire its bearer slot, then observe a deadline armed by a concurrent
response only inside an unconditional `await limiter.wait_retry_after()`.
That sleep retained the inflight slot and bypassed the original 25-second queue
deadline. The captured request finally relayed 503 after 1,004.484 seconds.

The account panel had a separate evidence gap. OAuth telemetry 429 metadata is
intentionally excluded from `bearer_state.last_ratelimit` so dashboard polling
cannot shrink or pause Messages traffic. A cold, un-routed account therefore
had no reset marker even when that same telemetry response proved a rejected
usage window. Treating every usage-endpoint 429 as an account lock would be
wrong because the endpoint can self-throttle while Messages traffic remains
usable.

### Fix and invariants

- `THROTTLE_UTILIZATION_TARGET` remains a soft routing/glide threshold. Crossing
  it performs the existing once-per-reset AIMD shrink and never writes
  Retry-After. Hard admission remains reserved for real Retry-After or unified
  `rejected` evidence.
- A deadline armed after slot acquisition makes the request give back its
  inflight slot and re-enter the original deadline-bounded routing/probe gate.
  It cannot sleep inside the slot, reset the queue deadline, or dispatch
  upstream first.
- The usage poller records `locked_until` only in its endpoint cache and only
  when the same 429 carries unified `rejected` plus Retry-After. A plain poll
  429 backs off polling but does not render an account lock. Neither path writes
  message limiter state, AIMD state, or `last_ratelimit`.
- The account panel says `usage locked · resets ...`, with a tooltip explicitly
  distinguishing the state from authentication failure. A future allowed
  window reset alone is no longer treated as lock evidence.

### Verification and remaining activation work

The deterministic post-slot race arms a 1,004-second deadline on the final
in-slot admission check and requires a response inside the harness bound, zero
upstream attempts, and zero queued/inflight leakage. The loopback telemetry
test drives two real proxy GETs with equal Retry-After values: only the
same-response rejected account renders locked, while both message limiters
remain at their initial caps with zero Retry-After and null `last_ratelimit`.
Targeted tests passed; all 501 tests passed both in a full all-core module shard
and a full sequential run; Ruff lint/format and `git diff --check` are clean.
The final GLM-5.2 different-family review returned `VERDICT: PASS` with no
BLOCKER or MAJOR finding after explicitly auditing the loopback state creation,
probe release, and original wait-deadline reuse.

After merge, the Prompts Codex tab owns the NixOS source/hash pin and guarded
desktop activation. Keep the temporary desktop `utilizationTarget=0` until the
new package is active. Runtime closure requires a fresh allowed 0.90 sample to
show glide without Retry-After, plus a fresh rejected telemetry 429 to show the
usage-lock marker; if the live row still lacks that marker, this fix is wrong.

---

## 18/07/2026 - OAuth telemetry isolation and attributable upstream failures

The remaining false-throttle path was in finalization, not admission. Requests
to `/api/oauth/usage` already bypassed the message limiter, but a telemetry 429
still fed its Retry-After/unified headers into bearer state and scheduled the
Groq advisor. A dashboard poll could therefore pause or shrink real
`POST /v1/messages` work and report a misleading concurrency diagnosis.

Telemetry paths now have one explicit invariant: their response metadata never
mutates message limiter state, AIMD, or the advisor. The advisor has the same
path guard at its own boundary so a future caller cannot reintroduce the
feedback loop. Normal message 429s still retain both AIMD and advisor behavior.

Upstream 400 diagnostics now carry method, path, status, bearer/client hashes,
route (`central`, `direct`, or `direct-fallback`), and model in both the reason
and completion logs. Error type/message fields are bounded and redact quoted or
unquoted Authorization, Basic/Bearer, API-key, OAuth-token, and known provider
key formats before logging. Nested error envelopes retain per-field top-level
fallbacks, central retryable-5xx errors no longer embed body snippets, and
central-to-direct retries update the route attribution. Telemetry keeps the same
relay-only behavior through direct fallback and body-bearing requests cannot
enter the Messages-only SSE keepalive path. Client-controlled correlation
fields are flattened and bounded before logging, preventing forged log lines.
Credential assignments use separate scheme/plain linear regex passes so hostile
whitespace cannot trigger polynomial backtracking.

Validation on the final tree: `480 passed` with the two pre-existing aiohttp
warnings, Ruff lint/format clean, `git diff --check` clean, and changed-file
duplication checks clean. Regression coverage includes hostile telemetry 429
headers, ordinary message-throttle advisor scheduling, 400 request attribution,
direct-fallback attribution, nested envelope fallbacks, and reflected-secret
forms including quoted API keys and Basic authentication.

## 17/07/2026-18/07/2026 - Central false-negative + 47-tab fleet recovery (issue #115, PR #114)

### Symptoms and verified split diagnosis

The incident contained two independent failures that initially looked like one:

1. Central `GET /__throttle/health` returned 503 after about 3.0s because the
   request handler performed an inline upstream DNS/connectivity probe with a
   1.5s timeout. Normal forwarded `/api/oauth/usage` requests took about 4.1s,
   so the health probe was guaranteed to produce false negatives on this path.
   Desktop then marked central down and sent work directly.
2. Anthropic returned the same headerless 429
   (`rate_limit_error`, `x-should-retry: true`, no `Retry-After` or unified
   headers) for a one-token Messages request on all three distinct OAuth
   organizations. The result was unchanged on the home IP and a ProtonVPN
   egress, and with both Opus and Fable. Anthropic's public status page was
   green. This rules out the proxy, one bearer, one model, and one source IP;
   it does not prove why Anthropic applied the shared upstream gate. The
   reconnect storm is a plausible trigger only, so this part remains
   partially verified rather than a claimed proxy fix.

### Durable central fix

PR #114 (`71d7f5596ad1668d22e4995375452f59cacf916b`) moved the upstream
egress probe out of the health request path and into one cached background
loop:

- health reads the cached result and never performs DNS;
- probe timeout is 10s, long enough for the measured upstream path;
- successful probes run every 30s;
- failures retry after 5s so a recovered upstream is not hidden for 30s;
- timeout/failure remains authoritative (`upstream_egress_ok=false`);
- lifecycle cleanup cancels the task, and unexpected loop errors are logged.

Validation was `462 passed` with the two pre-existing aiohttp warnings, plus
Ruff, mypy, CodeQL, Sonar, the repository quality gates, and three GLM-5.2
adversarial rounds. The first two review rounds caught real optimistic-timeout,
slow-recovery, and Dokku-config ordering defects; the final round approved.

The merged commit was deployed to Dokku container `22bf3027648`. Six live
central samples returned HTTP 200 in 3.2-4.4ms, reported
`upstream_egress_ok=true`, and showed `upstream_egress_last_check` advancing.
Desktop then reported `central_status=up`, all three bearers present, and an
empty queue. Dokku's future-start environment carries
`THROTTLE_UPSTREAM_HEALTH_TIMEOUT=10`.

### Full tab audit and recovery

Herdr was the sole active fleet. All 47 Claude panes were read; two additional
panes were Codex and unaffected. Eleven Claude panes had a current incident
failure:

- TLS/certificate path: `w1W:p3`, `w1X:p7`, `w1Y:p3`, `w22:p3`, `w24:p3`.
- Anthropic rate-limit path: `w1Y:p2`, `w10:p2`, `w10:p8`, `w21:p4`,
  `w21:p5`, `w24:p4`.

All eleven original tasks were restored under the explicit
`sonnet[1m]` selector. Running `/model sonnet[1m]` also saved that selector as
the global default for new Claude sessions. A real one-shot request through
the fallback returned `ONE_M_OK` before fleet use.

The fallback is a separate user service,
`zai-claude-throttle-proxy.service`, listening on `127.0.0.1:8767` and
forwarding to `https://api.z.ai/api/anthropic`. It is isolated with its own
`XDG_STATE_HOME`, hard-capped at two requests, starts AIMD at one, and uses a
25s queue bound. The Z.AI key comes from `rbw get api/zai`; it is never stored
in the service or transcript command line. During the recovery it served more
than 295 requests with no upstream retry or rate-limit event.

Three histories resumed directly. Eight older Anthropic histories contained
legacy thinking/tool payloads that Z.AI rejected with business error 1210.
Fresh 1M requests in the same projects succeeded, proving the projects and
tool schemas were not the cause. Removing hidden thinking blocks alone still
failed; text-history clones succeeded. The recovery therefore left every
original JSONL untouched, cloned the human/assistant text while omitting
legacy thinking/tool payloads, and supplied the latest verified task
checkpoint before continuing. This preserved the actionable context without
modifying the source transcripts.

The post-recovery fleet scan read all 47 Claude panes again and found no API,
TLS, rate-limit, or context failure in the latest output. Recovered work also
verified downstream progress: NextClient #337 and BridgeServer #258 merged,
and NixOS #1266 was already merged. The recovery service remains transient
across a user-manager reboot; the final return below retired it after the
normal endpoint canary proved the upstream gate had cleared.

### Final default-model return (18/07/2026)

The explicit `sonnet[1m]` recovery selector was incident-only. NixOS PR #1270
made every normal `claude` launch prepend `--model default --settings
~/.claude/ultracode.json`; caller arguments remain last so an intentional
one-off override still wins. The activated wrapper's real `execve` argv
contained both arguments. In this release the default resolves to Opus 4.8
with a 1M context window, and the settings file selects ultracode/xhigh effort.

All eleven affected panes were cleanly exited and resumed with their preserved
session UUIDs, without an explicit model argument and without the Z.AI endpoint
or token. Their live environment now points at the normal desktop proxy on
port 8765, and every status line reports Opus 4.8 (1M context) with
ultracode/xhigh. Two panes still displayed a historical Z.AI 1211 unknown-model
line in their scrollback; each returned `NORMAL_ENDPOINT_OK` through the normal
endpoint after resume, proving that the current request path—not merely the
status bar—works.

A final process-environment scan covered all 47 Claude panes and found no
remaining port-8767 or direct-Z.AI client. Desktop health then reported
`central_status=up`, `upstream_egress_ok=true`, and an empty queue. The
transient `zai-claude-throttle-proxy.service` was stopped; port 8767 is closed.

### Verified cross-service closure (18/07/2026 08:19 BRT)

The reason Sonnet 1M appeared during this incident was narrow: it was the
explicit selector used to prove and operate the temporary Z.AI recovery path
while Anthropic rejected the affected sessions. It was never intended to
remain the normal fleet default. The final state uses Claude Code's `default`
selector, which currently resolves to Opus 4.8 with a 1M context window, plus
ultracode effort.

The model/effort return is now durable rather than a one-session command:

- NixOS PR #1270 (`3ad67abd48cc58e3a5c26a9ac87163e475364174`)
  prepends `--model default --settings ~/.claude/ultracode.json` to ordinary
  launches while leaving explicit caller arguments last.
- NixOS PR #1280 (`a06aa45f2a18ec1415c16eb8c38596206ebcb6a2`)
  renders `{"env":{"CLAUDE_CODE_EFFORT_LEVEL":""},"ultracode":true}`. The
  empty environment value neutralizes stale project-local `max` settings that
  otherwise overrode ultracode. A preserved-session live test reproduced that
  precedence conflict before the change and reported ultracode/xhigh after it.
- The final Herdr census found 47 Claude panes. All 47 visible screens
  contained both `Opus 4.8 (1M context)` and `ultracode`; all 47 process
  environments used the normal local proxy on port 8765. Counts for port 8767,
  direct Anthropic, and missing endpoints were all zero. The two other panes
  were Codex and remained on their native `gpt-5.6-sol`/xhigh configuration.

Both Nix hosts are activated at the merged head, with live and boot profiles
matching:

- desktop: `/nix/store/qsjzq8bwsfidy6x7bqs570xk4myj5ly2-nixos-system-desktop-26.11.20260716.6368bc9`
- server: `/nix/store/4b1llc2xdz660c95qgzga34fz9k8bwac-nixos-system-server-26.11.20260715.753cc8a`

The server activation completed in 138 seconds. NixOS PR #1280 also set
`restartIfChanged=false` for the timer-driven self-hosted CI oneshot; its
`ExecMainStartTimestampMonotonic` remained `1879628237130` before and after
activation, proving activation did not restart the already-running full gate.
The exact-head gate had already passed all 49 checks and built the server
closure. PRs #1272 and #1276 also ratcheted dependency audits and made the
quality-gate hooks follow their stable managed profile paths across
activations.

Dokku recovery is durable through NixOS PR #1274
(`bc24ffb46a756c7c18441ab861ff1ad6f02e1f5a`) and the pure-build coverage in
PR #1278 (`841277c63cbe8012bca65a65e12443cee64158e5`). On the Dokku VM,
`dokku-proxy-resync.timer` is enabled/active, an explicit reconciliation ended
with `repairs=0 failures=0`, `dokku-redeploy.service` is a oneshot, and nginx
syntax passes. `anthropic-throttle`, `infisical`, `bridgeserver-dev`, and
`delicasa-unified` each report deployed=true and running=true. Live checks
returned 200 for central, Infisical, BridgeServer, and DeliCasa after its
intentional login redirect. Desktop health reports `central_status=up`,
`upstream_egress_ok=true`, zero inflight, and zero queued requests.

The same recovery audit found that the PostgreSQL incident on 17/07/2026 had
restored Nextcloud but left Dawarich freshly initialized. Dawarich was restored
from `/var/backup/postgresql/dawarich.prev.sql.gz`; the empty pre-restore state
is recoverable at
`/var/backup/postgresql/dawarich.empty-before-restore-20260718-101832.sql.gz`.
The live database contains 124,723 points and 5,631 visits, its API returns
200 with the existing vault token, and both web and Sidekiq are active. PR
#1278 adds an `ExecStartPre` guard that refuses backup rotation when the points
table is empty; the deployed guard was executed as the PostgreSQL user and
exited zero against the restored data.

### Reversal and recovery commands

Code rollback is one revert PR:

```bash
git revert 71d7f5596ad1668d22e4995375452f59cacf916b
```

The Nix-host changes are likewise one revert PR per merged slice; after merge,
activate the resulting generation or use `nixos-rebuild switch --rollback` for
an immediate host-generation rollback:

```bash
git revert a06aa45f2a18ec1415c16eb8c38596206ebcb6a2
git revert 841277c63cbe8012bca65a65e12443cee64158e5
git revert bc24ffb46a756c7c18441ab861ff1ad6f02e1f5a
```

Dokku can return to the previous container with:

```bash
ssh dokku@100.99.218.39 releases:rollback anthropic-throttle 1
```

The temporary fallback is stopped. If a future incident explicitly recreates
it, check or stop it without touching the normal desktop proxy:

```bash
curl -fsS http://127.0.0.1:8767/__throttle/health
systemctl --user stop zai-claude-throttle-proxy.service
```

Do not stop it while recovered tabs still point at port 8767. To canary the
fallback without exposing its credential:

```bash
zai_key="$(rbw get api/zai)"
ANTHROPIC_AUTH_TOKEN="$zai_key" \
  ANTHROPIC_BASE_URL=http://127.0.0.1:8767 \
  claude -p 'Reply exactly OK' --model 'sonnet[1m]'
```

---

## 17/07/2026 - Desktop reconnect-storm stabilization

### Symptom

After the fleet OAT deployment and desktop reboot, roughly 50 Claude panes
reconnected together. The desktop local tier saturated all three bearers at
the hot cap of 6 (`inflight=18`) while its queue grew from 24 to 51. Client
disconnects reached 255 lines in 10 minutes and caused a retry-amplified
backlog. Central stayed healthy with only 4 queued requests, and all three
OAuth windows remained `allowed` at 28-48% 5h utilization, so this was local
admission overload rather than budget exhaustion.

### Verified mechanism and live mitigation

Two conditions combined:

1. Cap 6 supplied only 18 fleet streams, below the reconnect burst's active
   demand.
2. The desktop unit still published `THROTTLE_QUEUE_MAX_WAIT_S=180`, unlike
   central's 25s bound. The running aiohttp 3.14.1 defaults
   `handler_cancellation=False`, and `proxy.main()` does not override it, so a
   TCP disconnect does not cancel a handler parked in the fair queue. The
   journal's repeated `client-disconnect where=post-queue` lines confirm those
   abandoned handlers later won a slot; the proxy's post-queue transport check
   correctly prevented upstream dispatch, but each stale waiter still had to
   consume an admission grant before live work advanced.

No code change was required. The existing `/ui/config` runtime-override path
was used to make these reversible changes without restarting live streams:

- desktop `queue_max_wait_s`: 180 -> 25
- desktop `max_concurrent`: 6 -> 8
- central `max_concurrent`: 6 -> 8

An intermediate cap-10 calibration was rejected: central POST-429 increased
147 -> 150 and bearer `b144f62f` acquired a new throttle timestamp. Both tiers
were immediately restored to cap 8. AIMD remained enabled throughout, but the
desktop's incident override (`backoff=5`, `ramp_after=1`) regrew quickly enough
that the hard ceiling was still the necessary safety bound.

### Verification

- Before mitigation: desktop `inflight=18`, `queued=48-51`; central
  `inflight=14`, `queued=4`; 11 local 429 lines, 44 queue-timeout lines, and
  255 disconnect lines in the preceding 10 minutes.
- Central disconnect baseline was 635 at the first capture and 665 immediately
  before tuning. It reached 680 early in the cap-8 calibration, then stayed
  flat; desktop disconnects likewise stayed flat at 346 for the 90s cap-8
  window.
- The cap-10 falsifier fired as described above. After restoring cap 8,
  central held `inflight=24`, `queued=0`, POST-429=150, and disconnects=680;
  desktop held `inflight=24`, queue 12-17, and disconnects=353 during the
  initial follow-up.
- A final four-minute cap-8 watch kept central POST-429 at 150 and disconnects
  at 680; its queue was 0 except one transient sample at 1. Desktop queue fell
  17 -> 4 while disconnects moved only 353 -> 354.
- Effective and persisted desktop systemd paths agree on package
  `xjcji67...anthropic-throttle-proxy-0.1.0`; no transient service override or
  restart was introduced.
- Desktop persistence was tested against the running package: with env defaults
  forced to `3/180`, `config.load_overrides()` loaded the state file and changed
  effective cap/queue wait to `8/25`.
- Central had no Dokku storage mount, so its hot override alone was not durable.
  `dokku config:set --no-restart` changed the future-start env cap from 3 to 8;
  the live container ID stayed `4b8595e8102`, proving no restart or stream cut.

### Current status

The disconnect storm stopped first, then the residual desktop queue completed
its drain. At `17/07/2026 17:43:59 -03`, desktop and central both reported
`inflight=11`, `queued=0`, cap 8; local POST-429 remained 48 and central
POST-429 remained 150. The incident is fully recovered. Cap 8 is the highest
verified ceiling; cap 10 is falsified for this workload.

### Reversal and persistence caveat

If new upstream pushback appears at cap 8, return both tiers to cap 6. Check
central health before using the hot endpoint:

```bash
curl -fsS -X POST http://127.0.0.1:8765/ui/config \
  --data-urlencode key=max_concurrent --data-urlencode value=6
if curl -fsS http://anthropic-throttle.home301server.com.br/__throttle/health \
  >/dev/null; then
  curl -fsS -X POST http://anthropic-throttle.home301server.com.br/ui/config \
    --data-urlencode key=max_concurrent --data-urlencode value=6
fi
ssh dokku@100.99.218.39 config:set --no-restart anthropic-throttle \
  CLAUDE_API_THROTTLE_MAX=6
```

Keep the desktop queue bound at 25s; restoring 180s would re-open the
disconnect-amplification path. The Home Manager unit still declares cap 3 and
queue wait 180, so the verified runtime state file currently reapplies 8/25
after desktop restart. Central's Dokku env now carries cap 8 and queue wait 25
for the next container. A future NixOS change should converge the desktop 25s
queue bound after the reconnect incident is fully cold; cap 8 can remain an
incident override until normal fleet demand is re-measured.

---

## 17/07/2026 - Safe synthetic probe + fleet OAT deploy (issue #107, PR #108)

### Symptom

After the A/B/C static-OAT migration, the one remaining unsafe diagnostic was
the synthetic probe path: it could still look like normal user traffic if a
future caller sent non-empty content through the probe helper. The fleet also
needed the #107 fix deployed through the Nix pin so every host ran the same
proxy build.

### Fix

PR #108 (`fix(throttle): gate synthetic one-token probe on claude-cli`) made
the probe opt-in on all of these gates:

- `THROTTLE_SYNTHETIC_ONE_TOKEN_PROBES=true`
- exact synthetic request shape
- empty text-only message payload
- `User-Agent` matching `claude-cli/*`

The message-length helper now only counts blocks whose `type` is `text`, so
non-text blocks cannot make a user request masquerade as an empty probe.

Merged:

- Proxy PR: https://github.com/yolo-labz/anthropic-throttle-proxy/pull/108
- Proxy merge commit: `4f51cb72660c1f1b50202f76b7ac0e6a6f556d8d`
- NixOS deploy PR: https://github.com/phsb5321/NixOS/pull/1258
- NixOS merge commit: `a6a8f843affc30117e0ee78beaad7e2850adbd21`

### Verification

- Proxy repo: `uv run pytest` -> 455 passed; `uv run ruff check .` -> clean.
- Cross-family adversarial review found no high/major blocker before merge.
- Nix package build carried the expected #107 runtime markers.
- Desktop switched to generation `1886`; local health showed
  `central_status=up`, `known=3`, `distinct=3`, `bearer_count=3`, all bearer
  statuses `allowed`.
- MacBook `nh darwin switch .` succeeded; launchd proxy uses
  `/nix/store/y6q8478g8adhdkvfrvvzzkp23yirvsh0-anthropic-throttle-proxy-0.1.0`,
  has all three static OAT files (`access_len=108`, `refresh_len=0`), and
  health reports `bearer_count=3`.
- Server `nh os switch .` eventually succeeded on
  `/nix/store/v2rsksvckp6arvjqbdgjb1iz0m9i0p1p-nixos-system-server-26.11.20260715.753cc8a`;
  user proxy uses the same #107 package as desktop, watcher/keepalive timers
  are active, and health reports `central_status=up`, `bearer_count=3`.

### Host activation repairs during deploy

The first server switch failed because unrelated services were dirty, not
because of the throttle package:

- root disk was tight; `nh clean` recovered enough space for activation
- `zellij-unwrapped-0.44.3` was copied from desktop's valid store output
- `podman-bookshelf.service` lacked `/mnt/torrents/data/torrents/ebooks`; the
  directory now exists as `qbittorrent:media`
- Nextcloud's live DB had zero tables while the data/config existed; restored
  `/var/backup/postgresql/nextcloud.sql.gz` from `17/07/2026 01:15` after
  saving the empty DB to a timestamped backup
- Syncthing completed its v2.1.2 DB migrations; `syncthing-init.service`
  starts cleanly again

Post-repair server state: `systemctl --failed` -> 0 units, no queued jobs,
`phpfpm-nextcloud`, `nextcloud-cron.timer`, `podman-bookshelf`,
`bookshelf-provision`, `syncthing`, `syncthing-init`, `selfhosted-ci.timer`,
and `audiobook-file-router.timer` all active.

### Known caveat

Desktop shows `account_identity.known=3/distinct=3` because it has identity
metadata. MacBook and server are OAT-only and Anthropic's OAT profile/usage
endpoints returned 403/429 during verification, so their health identity panel
shows `known=0` while the three bearer hashes and static OAT files are present.

---

## 15/07/2026 - Fresh-usage account routing + Anthropic partial outage (PR #104)

### Symptom

Most Claude Code panes showed:

```text
API Error: Server is temporarily limiting requests (not your usage limit) · upstream retry-after window is active; failing fast instead of holding the local gateway request
```

The local proxy also had stale per-bearer state from a previous upstream 429,
so requests that entered with bearer A (`2f41dc68`) were not reliably moving
to the lower-utilization bearer B (`7f082589`).

### Root cause

There were two independent failures:

1. **Local routing bug:** `_account_routing_candidate_score` treated
   `endpoint.err` containing `(429)` as a hard exclusion even when the account
   had a fresh successful `usage` snapshot. That made a routable account look
   unavailable until the stale endpoint error was cleared.
2. **Upstream outage:** direct calls to Anthropic with A, B, and C all returned
   fast headerless 429s. Anthropic status was `Partially Degraded Service` with
   incident `Elevated errors on multiple models`, status `identified`, updated
   `15/07/2026 11:49 -03`.

### Fix

PR #104 (`fix: keep fresh usage candidates routable`) changed the routing
score so endpoint `(429)` only excludes a candidate when there is no fresh
`usage` dict. It also preserves explicit API-key-only requests so account
routing does not rewrite them.

Merged:

- PR: https://github.com/yolo-labz/anthropic-throttle-proxy/pull/104
- Merge commit: `378d48d387dbf591be741a85b7c34b77fc3ca2cc`
- Merged at: `15/07/2026 11:51 -03`

### Verification

- `uv run pytest tests/test_proxy_helpers.py -k 'explicit_api_key or endpoint_429_keeps_fresh_usage_candidate or api_key_routing_prefer_rewrites_to_metered_key or account_routing_selects_least_loaded'`
  -> 4 passed.
- `uv run ruff check .` -> clean.
- `uv run pytest` -> 429 passed, 2 existing aiohttp UI warnings.
- CI passed after rerunning the Sonar job that initially failed on a transient
  SonarQube 502 upload error: `ruff + pytest`, `mypy`, `quality-gates`,
  `CodeQL`, and `SonarQube scan`.
- Live local venv imports the merged package and contains the fix:
  `fresh_usage_429_fix=True`.
- Post-restart journal showed the fixed route:
  `account-route from=2f41dc68 to=7f082589 label=B`.
- Local smoke through `http://127.0.0.1:8765/v1/messages` returned the current
  upstream 429 in `0.81s` with `x-anthropic-throttle-proxy: 1`, proving the
  proxy now fails fast instead of holding requests for 60-90 seconds.
- Local health after deploy: `central_status=up`, bearers
  `2586314b`, `2f41dc68`, `7f082589`, API-key routing disabled.
- Central Dokku deploy from `main` at `378d48d` passed both healthchecks.

### Live activation

Central:

```bash
cd ~/Documents/Code/yolo-labz/anthropic-throttle-proxy
git push dokku main
```

Dokku deployed `main -> main` at `378d48d` and reported:

```text
Healthcheck succeeded name='throttle ready'
Healthcheck succeeded name='port listening check'
Application deployed: http://anthropic-throttle.home301server.com.br
```

Local:

```bash
uv pip install --python ~/.local/state/anthropic-throttle-proxy/live-103-retry-after-revalidate-20260715001045/venv/bin/python \
  ~/Documents/Code/yolo-labz/anthropic-throttle-proxy
systemctl --user restart anthropic-throttle-proxy.service
```

### Temporary incident knobs

To stop nested local+central retry waits during Anthropic's headerless 429
incident, both tiers were set to fast-fail:

```text
THROTTLE_RATE_PUSHBACK_RETRIES=0
THROTTLE_QUEUE_MAX_WAIT_S=25
```

Local drop-in:

```text
~/.config/systemd/user/anthropic-throttle-proxy.service.d/31-fast-fail-incident-hotfix.conf
```

Central Dokku config was set with:

```bash
ssh dokku 'dokku config:set anthropic-throttle THROTTLE_RATE_PUSHBACK_RETRIES=0 THROTTLE_QUEUE_MAX_WAIT_S=25'
```

Restore after Anthropic status is healthy and a local `/v1/messages` smoke is
200 or a normal model error, not a headerless outage 429:

```bash
rm ~/.config/systemd/user/anthropic-throttle-proxy.service.d/31-fast-fail-incident-hotfix.conf
systemctl --user daemon-reload
systemctl --user restart anthropic-throttle-proxy.service
ssh dokku 'dokku config:set anthropic-throttle THROTTLE_RATE_PUSHBACK_RETRIES=2 THROTTLE_QUEUE_MAX_WAIT_S=180'
```

### API-key escape hatch

The Bitwarden entry `Claude Key` was checked via `rbw`, but the API-key route
is not usable right now: Anthropic returned `Your credit balance is too low to
access the Anthropic API`. The temporary API-key file and local prefer drop-in
were removed; current health reports `api_key.enabled=false` and
`api_key.routing=off`.

### Current state

The proxy-side bug is fixed, merged, deployed centrally, and active locally.
Remaining Claude pane failures are expected until Anthropic clears the
`15/07/2026` service incident or the accounts stop receiving headerless
upstream 429s.

---

## 14/07/2026 - Usage-poll AIMD poisoning (branch 096) — completes the 13/07 incident

### Symptom

The 13/07 "Usage refresher Retry-After storm" was declared closed under
branch 095 (PR #101), but the journal still showed an hourly
`rate-pushback-retry` / `aimd-shrink` / `retry-after-fast-fail` cycle, all on
`path=/api/oauth/usage`. Each cycle collapsed the workhorse bearer
(`e1241e10`) to `max_concurrent=1` and parked it ~58 min, so real
`/v1/messages` calls fast-failed (`source=pre-dispatch` and
`queue-timeout-relay status=503`) — the "failing fast instead of holding the
local gateway request" banner.

### Root cause — what #101 missed

PR #101 made the account usage *refresher* honor `Retry-After`
(`_refresh_one` backoff + `_retry_after_remaining_for_token` pre-check),
which killed the 90 s re-poll storm. But each remaining poll still routed
through `handler` (`accounts.py::_oauth_base → 127.0.0.1`) and its 429 flowed
through the **same** `_forward_with_retry` pushback branch + `_finalize`
AIMD-feedback as a `POST /v1/messages` 429. No path-awareness existed, so one
limiter collapse/hour remained. The 429 is the `/api/oauth/usage` endpoint's
OWN rate limit (unified headers: `util_5h=0.01`, `status=allowed`) — orthogonal
to the message quota the AIMD machinery protects.

### Fix (PR #102, branch `096-telemetry-aimd-exempt`)

Exempt `/api/oauth/usage` + `/api/oauth/profile` telemetry reads from the
message-dispatch pushback/AIMD/fast-fail machinery, at BOTH AIMD-affecting
sites:

- `_forward_with_retry` pushback branch (retry / fast-fail / `retry_after_until`)
- `_finalize` finally-block `_aimd_feedback` (shrink)

via `_is_oauth_telemetry_path(path)`. The 429 is relayed unchanged to the
poller, which already backs off via #101. Unified-utilization gauges still
update via `_apply_unified`, so budget pacing/glide is unaffected.

### Verification

- `uv run pytest -q` → 420 passed (+2 new), 2 pre-existing aiohttp warnings.
- `uv run ruff check src tests` / `ruff format --check` → clean.
- New tests force a 429+Retry-After:3600 on the telemetry path and assert
  `lim.max_concurrent` unchanged + `lim.retry_after_remaining()==0`:
  `test_oauth_usage_429_does_not_poison_message_limiter`,
  `test_oauth_profile_429_also_skips_message_limiter`.
- Message-path shrinking unchanged (`test_429_triggers_aimd_shrink`,
  zai-quota, concurrency-429).
- **Live (hotfix PID 938581):** a `GET /api/oauth/usage` poll completed with
  zero `aimd-shrink`/`rate-pushback`/`retry-after-fast-fail`; the handling
  bearer stayed `max_concurrent=3, rau=0`; the workhorse restored
  `max_concurrent` 1→3 after the restart.

### Live activation

State-dir venv from branch 096 (same reversible pattern as 094/095):

- package symlink:
  `~/.local/state/anthropic-throttle-proxy/live-096-telemetry-exempt-current`
  → `live-096-telemetry-exempt-20260714102454`
- drop-in (replaces the 095 one):
  `~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-telemetry-aimd-exempt-hotfix.conf`
- live PID: `938581`, active since `14/07/2026 10:24 -03`
- fixed symbol verified in the active package: `_is_oauth_telemetry_path`
  (both gates, proxy.py:2180 + proxy.py:2533)

Rollback:

```bash
rm ~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-telemetry-aimd-exempt-hotfix.conf
systemctl --user daemon-reload
systemctl --user restart anthropic-throttle-proxy.service
```

### Adversarial review

Cross-family review was BLOCKED at activation time (Codex quota-locked until
Jul 19; Claude CLI fast-failed on the workhorse's pre-restart retry-after).
**Merge of PR #102 is gated on the review completing** — retry via the proxy
once a bearer is usable, or via Codex after Jul 19. Do NOT merge before
attaching the review.

### Durable follow-up

Merge #102, bump the NixOS package pin to the merged SHA, and remove the
emergency `20-telemetry-aimd-exempt-hotfix.conf` drop-in after the Nix/HM
unit points at a store path containing `_is_oauth_telemetry_path`. Then
remove the now-superseded 095 venv (`live-095-usage-retry-after-*`).

---

## 13/07/2026 - Usage refresher Retry-After storm (branch 095)

### Symptom

After PR #100 removed generation fast-fail on retry-aftered bearers, Claude
Code panes still showed many transient retry banners. The local service was not
using the billing-gated API-key route (`api_key.enabled=false`,
`routing="off"`), central was healthy, and `/v1/messages` requests were
completing through account C. The fresh 429 source in the journal was instead
the dashboard/account refresher:

- `GET /api/oauth/usage` for A/B hit long `Retry-After` windows.
- The proxy logged `retry-after-fast-fail path=/api/oauth/usage`, then
  `rate-pushback-retry` / `aimd-shrink`.
- The refresher retried again after its fixed 90 s failed-poll backoff, even
  when the upstream/proxy retry-after window was still tens of minutes long.

### Fix

Branch `095-usage-retry-after` makes the account usage refresher honor
`Retry-After` in two places:

- `_get_json()` now returns parsed `Retry-After` seconds for non-200 responses,
  including HTTP-date header values.
- `_refresh_one()` backs failed usage polls off for
  `max(ENDPOINT_TTL_S, Retry-After)`.
- Before the first GET after a restart, `_refresh_one()` checks the persisted
  proxy retry-after state for the credential bearer id and skips polling a
  still-paused account.

The UI/account panel can still show stale endpoint data and a clear
`usage endpoint paused by retry-after (...)` note, but it no longer creates
extra 429/AIMD noise while the operator is trying to keep generation flowing.

### Verification

- `uv run pytest tests/test_accounts.py -q` -> `51 passed`, 1 existing
  aiohttp `NotAppKeyWarning`.
- `uv run ruff check src tests` -> clean.
- `uv run ruff format --check src tests` -> clean.
- `uv run pytest -q` -> `418 passed`, 2 existing aiohttp warnings.

### Live activation

Built and activated a state-dir venv from this branch:

- package symlink:
  `~/.local/state/anthropic-throttle-proxy/live-095-usage-retry-after-current`
- persistent drop-in:
  `~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-usage-retry-after-hotfix.conf`
- effective `ExecStart`:
  `~/.local/state/anthropic-throttle-proxy/live-095-usage-retry-after-current/venv/bin/anthropic-throttle-proxy`
- live PID: `3173109`, active since `13/07/2026 23:11:23 -03`
- fixed symbol verified in the active package:
  `_retry_after_remaining_for_token`

Live sample after restart:

- explicit `/ui/stats` render produced no `/api/oauth/usage` 429s.
- 95-second sample from `13/07/2026 23:12:08 -03`:
  `/api/oauth/usage=0`, `status=429=0`, `retry-after-fast-fail=0`,
  `rate-pushback=0`, `aimd-shrink=0`, `status=503=0`, `status=529=0`.
- active health during traffic:
  `central_status=up`, `queued=0`, `upstream_retries=0`,
  `client_disconnects=0`, `api_key.enabled=false`,
  `api_key.routing=off`, `account_identity.distinct=3`.
- `/v1/messages` completed with `retries=0`.

Rollback for the emergency activation:

```bash
rm ~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-usage-retry-after-hotfix.conf
systemctl --user daemon-reload
systemctl --user restart anthropic-throttle-proxy.service
```

Durable follow-up: merge branch 095, bump the NixOS package pin, and remove
the emergency venv drop-in after the Nix/HM unit points to a store path
containing `_retry_after_remaining_for_token`.

---

## 12/07/2026 - Warning-account backpressure valve (branch 094)

### Symptom

After the API-key path was kept disabled and the fleet was returned to
subscription/OAuth traffic, several zellij panes still showed Claude Code's
transient API retry banner. Local health showed no API-key route and no
credit-balance failure, but local queue depth stayed high:

- `api_key.enabled=false`, `api_key.routing="off"`.
- `max_concurrent=1..3` during live mitigation.
- A/B were `allowed_warning` at about `u7=0.75`; C was `allowed` around
  `u7=0.59`.
- The router kept selecting C, so C accumulated double-digit local queue while
  A/B sat idle.
- Journal lines were `queue-wait-timeout ... bid=28237b9b ...`, not upstream
  budget 429s.

### Hypothesis and fix

Hypothesis: `budget_paced` strict candidate selection treated
`allowed_warning` as an absolute exclusion whenever any non-warning account
existed. Under fleet burst, that made C the only strict candidate even after it
had enough queued work to surface client retries.

Branch `094-warning-backpressure` keeps `rejected`, `Retry-After`, and endpoint
`429` accounts hard-excluded, but changes warning accounts from an absolute
fallback-only exclusion into a finite backpressure surcharge. Warning accounts
still lose under light load, but can win when the non-warning account has
several queued local requests.

Regression tests added:

- warning account loses while the safe account has only light queue
- warning account wins once the safe account has enough queue backpressure
- endpoint `429` remains hard-excluded, including the stale-cache shape where
  the account usage refresher preserves prior `usage` while setting
  `err="...(429)"`

Validation in the branch:

- `uv run pytest tests/test_proxy_helpers.py -q` -> `81 passed`
- `uv run ruff format --check src tests` -> clean
- `uv run ruff check src tests` -> clean
- `uv run pytest -q` -> `408 passed`, 2 existing aiohttp `NotAppKeyWarning`
  warnings
- Codex adversarial review found one real MAJOR before merge: endpoint `429`
  was only hard-excluded when cached `usage` was absent. Fixed by checking
  `endpoint.err` before the `usage` branch and adding the stale-cache
  regression above. Re-validated with the commands in this section.

### Live activation

For immediate recovery, a state-dir venv was built from the branch and the user
service was restarted through a persistent drop-in:

- package symlink:
  `~/.local/state/anthropic-throttle-proxy/live-094-warning-backpressure-current`
- drop-in:
  `~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-warning-backpressure-hotfix.conf`
- effective `ExecStart`:
  `~/.local/state/anthropic-throttle-proxy/live-094-warning-backpressure-current/venv/bin/anthropic-throttle-proxy`
- fixed symbol verified in the active package:
  `_WARNING_BACKPRESSURE_SURCHARGE`

Immediate health after restart showed the queue distributed across configured
accounts instead of concentrating on C:

- `served=1`, `queued=4`, `inflight=9`
- A/B/C each `inflight=3`; queues `2/1/1`
- `upstream_retries=0`, API-key route still off

90-second live sample after restart:

- `served=31`, `queued=3`, `inflight=8`
- no `status=429`, `rate-pushback`, `aimd-shrink`, or `queue-wait-timeout`
  after the restart window
- account-route logs show traffic reaching B and C; A continued serving direct
  incoming work
- the remaining affected panes moved past the API error banner; Pearson Clever
  is blocked on the WSL tunnel, not the proxy

180-second live sample after restart:

- `served=82`, `queued=3`, `inflight=4`
- counts since the fixed PID started:
  `status429=0 rate_pushback=0 aimd_shrink=0 queue_wait_timeout=0 api_key_route=0 credit_balance=0`

After the adversarial-review endpoint-cache fix, the emergency venv was rebuilt
and the service restarted again:

- PID `1331010`
- fixed symbols verified in the active venv:
  `_WARNING_BACKPRESSURE_SURCHARGE` and the pre-`usage` endpoint `(429)` check
- 90-second sample from that PID:
  `served=22`, `queued=3`, `inflight=5`, `upstream_retries=0`
- counts from that PID:
  `status429=0 rate_pushback=0 aimd_shrink=0 queue_wait_timeout=0 api_key_route=0 credit_balance=0`

Rollback for the emergency activation:

```bash
rm ~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-warning-backpressure-hotfix.conf
systemctl --user daemon-reload
systemctl --user restart anthropic-throttle-proxy.service
```

Durable follow-up: merge branch 094, then bump the NixOS package pin and remove
the emergency venv drop-in after the Nix/HM unit points to a store path
containing `_WARNING_BACKPRESSURE_SURCHARGE`.

## 12/07/2026 - Throughput 429 incident: known-bearer preserve guard (branch 093)

### Live diagnosis

The 14:00 fleet storm showed 12 bearer rows in local health, but only three
configured local credential stores (`A/B/C`) and three distinct account
identities. The other bearer ids are historical/incoming client tokens the
process has observed, not credentials the local router can mint or refresh.
Central is healthy (`max_concurrent=8`, queued 0) but has no credential files,
so it can admit/queue per bearer; it cannot rewrite desktop traffic onto
bearers it does not own.

At the emergency-safe live setting (`max_concurrent=1`,
`min_dispatch_gap_ms=100`, `THROTTLE_UTILIZATION_TARGET=0.9`) the fleet has
zero new `status=429`/`rate-pushback`/`aimd-shrink` lines after 14:14:06 -03,
but the true free ceiling is only three active upstream requests: one per
configured OAuth account. Raising per-bearer concurrency was already falsified
live by fresh 429s.

### Branch 093 fix scope

Branch `093-preserve-known-bearers` keeps the existing `least_loaded` account
routing for configured accounts, but adds a narrow preserve path for incoming
non-configured OAuth bearers that are already known, have fresh unified-budget
telemetry, are below `UTILIZATION_WARN`, have no local `Retry-After`, and are
not locally more loaded than the best healthy configured account. Stale,
pressured, retry-aftered, or already-loaded known bearers still fall back to
configured A/B/C routing.

This prevents the router from unnecessarily collapsing a live healthy client
bearer onto A/B/C, without resurrecting arbitrary stale tokens or regressing
least-loaded behavior under queue/inflight pressure. It does not create new
free capacity when current clients only send A/B/C tokens.

### Verification

- Host run: `uv run pytest tests/test_proxy_helpers.py -q` -> 68 passed.
- Host run: `uv run pytest -q` -> 395 passed, 2 existing aiohttp warnings.
- Host run: `uv run ruff check src tests` -> all checks passed.
- Codex adversarial review round 1 found a real P2: preserve path ignored
  local queued/inflight load. Fixed by `_bearer_local_load_score` and a
  parameterized regression test.
- Codex adversarial review round 2: no actionable correctness issues.

Remaining throughput levers are outside this code slice: add more active
distinct OAuth credential stores, enable the sanctioned API-key bearer (billing
gated), or reduce per-request model/output weight.

## 10/07/2026 - Distinctness-guard false positive (stale `.claude.json` label) → PR #90 verify-before-warn

### Symptom

The #86 distinctness guard (spec 085 FR-005) warned that two credential
stores held the SAME Anthropic account — the exact signature of the 09/07
mutual refresh-token-revocation outage. Alert read as "re-auth needed NOW".

### Root cause — the guard trusted a label that lies after promote

`claude-account-promote` swapped `.credentials.json` between stores at
10:22:07 but did NOT swap the adjacent `.claude.json` (`oauthAccount.emailAddress`).
The guard read that stale label 7 s later (10:22:14) and reported a
collision. Live `/api/oauth/profile` probes of all three tokens showed
**three distinct accounts** — no collision, no re-auth needed. The stale
window lasted ~2.6 h until diagnosed. False alarm class: **metadata drift**,
not credential drift.

### Fix shipped

- **Proxy PR #90 (this repo)** — verify-before-warn. Email provenance is now
  explicit: `guard_email()` returns `(email, verified)`; only a mtime-fresh
  `/api/oauth/profile` result counts as verified; the `.claude.json` label is
  UNVERIFIED. Shared-email groups with any unverified member land in a new
  `suspected` verdict bucket (health JSON + `anthropic_account_identity_suspected`
  gauge, DISTINCT gauge reads -1 while pending) instead of `duplicates`. A
  background task live-probes the suspects (2 attempts, singleflight per
  path, TOCTOU-guarded against mid-probe rotation) then: verified duplicate →
  the old loud warning; probe dead → warn "(unverified)"; distinct → one
  "cleared by profile probe" log line and no alarm. `M_ACCOUNT_COLLISIONS`
  counts VERIFIED collisions only; alert story = collisions>0 OR suspected>0.
  Codex adversarial review: round 1 = 3 MAJOR + 1 MINOR (TOCTOU, task-dedupe
  pop race + probe singleflight, gauge semantics regression, debounce gap) —
  all fixed; round 2 re-verify = Findings 1–3 RESOLVED, Finding 4 residual
  closed via `emitted_sus` once-per-epoch gate (586d0d6).
- **NixOS PR #1188 (root cause)** — `claude-account-promote` now swaps
  `.claude.json` `oauthAccount` SYNCHRONOUSLY with `.credentials.json` inside
  the same locked/trapped block; rollback restores both. The proxy-side #90
  guard hardening is belt-and-suspenders for any OTHER writer that drifts the
  label.

### Durable lessons

1. **`.claude.json` `oauthAccount.emailAddress` is a display label, not an
   identity.** Any identity decision must come from a live `/api/oauth/profile`
   probe of the token in `.credentials.json` (routed via the proxy — #87
   self-429 class).
2. **Warn-paths need provenance.** An alarm built on unverified metadata must
   say so (or verify first). The 09/07 outage made this guard's warning
   high-stakes; a false positive here burns a Pedro-gated re-auth cycle.
3. **Verification must never block `/__throttle/health`** (invariant #4) —
   probes run in a background task; health returns `suspected` immediately.

---

## 04-05/07/2026 - Account-routing series shipped; 7d budget brake stays on

### What shipped (all merged, CI + CodeQL + Sonar green at `f984c18`)

- **#76** — 502 responses no longer echo upstream exception detail.
- **#77** — opt-in local account routing (`THROTTLE_ACCOUNT_ROUTING=least_loaded`
  + `THROTTLE_ACCOUNT_CRED_PATHS`): the proxy picks the least-loaded usable
  credential per `POST /v1/messages` and rewrites only the upstream
  `Authorization` header. Also defaulted `THROTTLE_PRIORITY_RESERVE_SLOTS` to 0
  and added the fake-Anthropic simulator + k6 workload docs.
- **#78** — routing excludes `allowed_warning`/pressured accounts; queued
  requests re-check long `Retry-After` after getting a slot and fast-fail
  instead of sleeping in the only slot.
- **#79** — routing also consults cached per-endpoint 429/`rejected` state
  (`_fresh_endpoint_entry` in `accounts.py`).
- NixOS twins: #1141 (routing env in the HM module) → #1143 → #1144
  (pin `rev = f984c18`), all merged in `~/NixOS` main.

### Deployment state (verified 04/07 ~23:30 -03)

- **Desktop**: HM activation deferred (niri host), so the base unit still
  points at a pre-#79 build. Bridged by the persistent drop-in
  `~/.config/systemd/user/anthropic-throttle-proxy.service.d/20-account-routing-hotfix.conf`
  (cred paths + `least_loaded` + `ExecStart` pinned to the #79 store path,
  symbol-verified) and the gcroot
  `~/.local/state/anthropic-throttle-proxy/live-warning-fallback-package`.
  Reboot-safe. The next `nixos-smart-switch` / `nh os boot` + reboot makes the
  base unit current; do NOT run a live switch mid-storm — it restarts this
  user service and kills in-flight Claude streams.
- **Central Dokku**: was 4 PRs behind (container built 03/07 right after #74,
  so the #76 leak fix was not live). Redeployed 04/07 23:32 via
  `git push dokku main` (`a23e584..f984c18`), zero-downtime checks passed,
  root probe 200 in 3 ms, local `central_status=up`. Central keeps routing
  OFF by design (no credential files there — per #77 deploy notes).
- Both credential files alive and DISTINCT accounts again (B was re-logged
  after the 03/07 refresh-token wipe; utils differ, so routing is
  live-effective).

### The budget brake (do not loosen without redoing this math)

`overrides.json` still carries the 03/07 storm clamp: `max_concurrent=1`,
`central_local_max_concurrent=1`, `min_dispatch_gap_ms=500`, `ramp_after=20`,
`decrease=0.6`. Overrides WIN over env on restart.

04/07 23:30 evidence: active account 5h=0.30 but **7d=0.69 at only ~24% of the
7d window elapsed** (window started 03/07 09:20 -03) — a burn pace of ~2.8×
sustainable, projecting a 7d wall within ~1 day. The other account already sits
at 7d=0.99 and is auto-excluded by #78. A 7d `rejected` means a **days-long**
outage, unlike a 5h wall. So the clamp is doing exactly its job; the visible
cost is client disconnects climbing (tabs time out while queued; #68 drops
them pre-dispatch so no quota is burned). Loosening the clamp trades a
days-long fleet outage tomorrow for tab latency today — don't.

Relax criteria: BOTH accounts' 7d below ~0.8 AND burn pace ≤ ~1.15×
(`util_7d ÷ elapsed-window-fraction`). Then restore
`central_local_max_concurrent=2` and `min_dispatch_gap_ms=50..100` via
`POST /ui/config` (hot, persists to overrides.json).

### Priority lane enabled (05/07)

Set `priority_reserve_slots=1` via `/ui/config` (persisted). Short no-tools
calls (≤8192 max_tokens, ≤256 KB body) — Stop-hook evaluators, titles — now
dispatch from the dedicated slot instead of starving behind long Opus/Fable
generations at 1-slot concurrency. Negligible 7d burn; total upstream
concurrency is now ≤ live cap (1) + reserve (1).

Known observability gap: the priority lane exposes no counters in
`/__throttle/health` or `/metrics` — you cannot yet distinguish lane
dispatches from main-pool dispatches without reading the queue logs. Add
gauges/counters if the lane's behavior comes into question.

## 30/06/2026 - OpenCode z.ai Coding Plan throttle split

### What changed

OpenCode desktop tabs now share one z.ai Coding Plan bearer via the throttle
proxy. z.ai sends at least two distinct 429 classes:

- `1302`: concurrent-request pushback. This remains an AIMD signal and shrinks
  the live per-bearer ceiling.
- `1308` / `1316` / `1317`: plan quota windows. These are quota gates, not
  evidence that concurrency is too high. z.ai puts reset data in the JSON body
  rather than a `Retry-After` header, so the proxy parses `reset_time` /
  `resetAt` variants and the older `message` text into a local retry-after
  window.

Default resume jitter is `THROTTLE_ZAI_QUOTA_RESET_JITTER_S=15` seconds so all
tabs sharing one key do not resume at the exact reset second.

### Local endpoint contract

For the desktop OpenCode client, run a dedicated local instance with:

```sh
THROTTLE_UPSTREAM=https://api.z.ai/api/coding/paas/v4
CLAUDE_API_THROTTLE_MAX=2
THROTTLE_AIMD_INITIAL_CONCURRENT=2
THROTTLE_QUEUE_MODE=fair
THROTTLE_HOST=127.0.0.1
THROTTLE_PORT=8766
```

Then set OpenCode `provider.zai-coding-plan.options.baseURL` to
`http://127.0.0.1:8766`.

---

## 29/06/2026 - Fleet Claude credential sync to active usable account

### What was wrong

Pedro asked to update Claude credentials across the other hosts, ProxMox, and
WSL surfaces. The local account picker showed the active usable account was
account A:

    A(fd332a33): 5h=31% 7d=7% pace=1.75x | B(a8060655,use=1): 5h=31% 7d=7% pace=1.75x | pick=a

The active credential file was `~/.claude/.credentials.json`, bearer
`fd332a33`, expiring `29/06/2026 18:08 -03`. Pre-sync probes found drift on
several reachable hosts:

- `DT-SCI018` / Pearson WSL was already current: `fd332a33`.
- `ProxMox` had stale bearer `cd8d8f33`.
- `ProxMox.Dokku` had stale bearer `c3e6a625`.
- `Nix.Server` and the alternate `ProxMox.NixOS` route had stale bearer
  `9a3192ff`.
- `ProxMox.Runner` had stale bearer `db5a325b`.
- `Mac.Pro` had Claude installed but no `~/.claude/.credentials.json`.

Unreachable or intentionally skipped during this pass:

- `Mac.Air`: SSH failed with `Permission denied (publickey,password)`.
- `LAN.ProxMox` root alias: local `/home/notroot/.ssh/id_rsa` was missing and
  root SSH failed public-key auth.
- `Pi`: no Claude binary and no credential file.
- Several LAN-only ProxMox VM aliases were unreachable from the current network
  (`No route to host`) or denied the configured key.

### What was repaired

- Synced the active A credential (`fd332a33`) to:
  - `ProxMox`
  - `ProxMox.Dokku`
  - `Nix.Server`
  - `ProxMox.Runner`
  - `Mac.Pro`
- Preserved each target's existing credential/config as timestamped backups
  before writing:
  - `~/.claude/.credentials.json.bak-<timestamp>`
  - `~/.claude.json.bak-<timestamp>`
- Updated each target's `~/.claude.json` `oauthAccount` metadata to match the
  active account (`pedrobalbino@proton.me`) while preserving the rest of the
  target config.
- Re-ran `pearson-claude-token-sync`; the Pearson WSL credential stayed on
  `fd332a33`.
- `ProxMox.Runner` initially could not accept the upload because `/` was at
  `100%` with `0` available blocks. Before syncing it, freed only unopened
  runner temp/cache artifacts:

      /tmp/.claude-active-cred.json
      /tmp/gitleaks.tar.gz
      /home/notroot/actions-runner-pidashboard-vm103/_work/_temp/79c4df5d-6be0-44f7-8bed-bdee77afda9a/cache.tzst
      /home/notroot/actions-runner-piorchestrator-vm103/_work/_temp/fd88bff9-b579-4cc3-a6c5-cacaf2ca6c0e/cache.tzst

  There was no active `Runner.Worker`, and `fuser` reported no users for the
  two `cache.tzst` files. Free space moved from `0` to `3.1G`; after sync the
  runner still had `2.3G` free.

### Final verification

- `claude-account-pick status` still selected A (`fd332a33`), with 5h `31%`
  and 7d `7%`.
- Local proxy health after the sync:

      central=up queued=0 active=fd332a33 status=allowed status_7d=allowed util5=0.33 util7=0.07 retry_after=null

- Post-sync probes showed all reachable targets on bearer `fd332a33`:
  - `DT-SCI018`
  - `ProxMox`
  - `ProxMox.Dokku`
  - `Nix.Server`
  - `ProxMox.NixOS`
  - `ProxMox.Runner`
  - `Mac.Pro`
- Claude smoke tests passed on hosts where Claude is installed:

      DT-SCI018 smoke=ok output=CRED_OK
      Nix.Server smoke=ok output=CRED_OK
      Mac.Pro smoke=ok output=CRED_OK

Rollback for any synced host is to restore the host-local timestamped backup
created during this pass, then rerun the credential probe and a Claude smoke
test if that host has Claude installed.

---

## 27/06/2026 - Zellij fleet recovery and Pearson remote-token drift

### What was wrong

Pedro reported that some Pearson and DeliCasa zellij tabs still had problems
after the server/gateway repair. The host-side zellij sessions were not live:
`DeliCasa` and `Pearson` were saved as exited sessions, while `HOME` was the
current session. Historical pane dumps showed the affected Pearson panes had
the managed credential nudge:

    Please run /login · API Error: 401 throttle-proxy: active account changed; re-read credentials

The local proxy was healthy (`central=up`, `queued=0`, egress OK), so this was
not a queue/concurrency incident. Pearson had an additional remote-token drift:
the corp WSL box's `~/.claude/.credentials.json` hashed to bearer `a3ad80d7`,
which matched neither the local active bearer (`3724b5ae`) nor the local
standby bearer (`0e424d0a`) at the time. The recurring
`pearson-claude-token-sync` helper was also unsafe for the new account model
because it hardcoded `~/.claude-b/.credentials.json`, even though account B was
then exhausted/rejected and account A was the usable active slot.

### What was repaired

- Backed up and synced the Pearson corp WSL credential to the current active
  local account, then synced the non-secret `oauthAccount` metadata block.
  Verification on the corp box showed bearer `3724b5ae` and a future
  `expiresAt`.
- Ran a Pearson-side Claude smoke test:

      claude -p "Reply PEARSON_OK only." -> PEARSON_OK

- Patched `/home/notroot/.local/bin/pearson-claude-token-sync` so future runs
  default to the active desktop credential:

      SRC="${PEARSON_CLAUDE_TOKEN_SRC:-$HOME/.claude/.credentials.json}"

  `PEARSON_CLAUDE_TOKEN_SRC` remains an explicit override for intentional
  one-off syncs.
- Restarted the user timer. Final state:
  `pearson-claude-token-sync.timer` was `active (waiting)` with the next trigger
  scheduled for `27/06/2026 07:01 -03`.
- Resurrected the saved `Pearson` and `DeliCasa` zellij sessions with
  `zellij attach --force-run-commands`, then closed only the temporary attach
  clients. No broad `pkill`, `killall`, proxy restart, or
  `claude-account-promote --now` was used.
- Sent targeted resume prompts only to the panes that needed work:
  - Pearson `terminal_1` / `🛒 Ecom`: resume the failed 26/06/2026 ecommerce
    meeting/vault update task.
  - Pearson `terminal_2` / `🔷 Clever`: resume the failed 26/06/2026 Clever
    meeting/vault update task.
  - DeliCasa `terminal_0` / `🏠 Root`: resume fleet close-out coordination.
  - DeliCasa `terminal_6` / `📊 PiDash`: resume the PiDashboard blocker with
    evidence-first CI/local repro work.
- Left the other visible panes alone because scanners reported no stuck API
  banner:
  - Pearson Root and Gauge.
  - DeliCasa NextClient, BridgeServer, Wire, ESP32, and PiOrch.

### Final verification

- Host-side `zellij list-sessions` showed `HOME`, `DeliCasa`, and `Pearson`
  live.
- `NUDGE_SESSION=Pearson claude-tabs-nudge --all` ->
  `scanned=3 claude=3 stuck=0`.
- `NUDGE_SESSION=DeliCasa claude-tabs-nudge --all` ->
  `scanned=6 claude=6 stuck=0`.
- `CYCLE_SESSION=DeliCasa claude-tabs-cycle --dry-run` ->
  `scanned=6 stuck-cyclable=0`.
- Pane dumps after the targeted prompts showed no residual
  `Please run /login` / `401 throttle-proxy` banner:
  - Pearson Ecom returned to a live prompt after completing its check
    (`Cooked for 41s`).
  - Pearson Clever returned to a live prompt after its vault update/check
    (`Worked for 1m 17s`).
  - DeliCasa Root was actively working the PiDashboard fleet blocker.
  - DeliCasa PiDash was actively running its CI/local repro shell.
- Local proxy health after the prompts: `central=up`, egress OK,
  `queued=0`, active bearer `3724b5ae` had no `retry_after`.

Rollback for the live Pearson token-sync helper patch is:

    sed -i 's#SRC="${PEARSON_CLAUDE_TOKEN_SRC:-$HOME/.claude/.credentials.json}"#SRC="$HOME/.claude-b/.credentials.json"#' ~/.local/bin/pearson-claude-token-sync

Only use that rollback if account B is intentionally the desired Pearson
source again; otherwise it reintroduces the stale-account failure mode.

---

## 27/06/2026 - Pearson tabs had stale Claude credentials after account collapse

### What was wrong

Pedro reported that the Pearson zellij session still had Claude Code access
errors after the tabs were restarted. The visible errors were:

    API Error: Server is temporarily limiting requests (not your usage limit) · upstream retry-after window is active; failing fast instead of holding the local gateway request
    Please run /login · API Error: 401 throttle-proxy: active account changed; re-read credentials

The proxy was not wedged. The live issue was local account identity state:

- `claude-account-pick status` reported both A and B as the same account
  (`id=0`) even though B's profile metadata still said
  `pedrobalbino@pm.me`.
- The live B credentials file hashed to the same OAuth bearer as A, so account
  switching was cosmetic and half of the fleet capacity was gone.
- Historical B backups had dead refresh tokens (`invalid_grant`), so restoring
  an old credentials file would only have recreated a broken login.
- `claude-tabs-cycle` correctly refused to treat all Pearson panes as
  cyclable; some were already at a shell or no longer showed a stuck banner in
  the scanner.

This was a credential-profile collapse, not a queue/concurrency bug in
`anthropic-throttle-proxy`.

### What was repaired

- Backed up the current B credential/profile files before changing them.
- Re-authenticated `~/.claude-b` as `pedrobalbino@pm.me` through a dedicated
  browser profile, instead of using `/login` inside a running Claude TUI.
- Verified `claude-account-pick status` returned distinct identities again:
  A stayed `id=0`; B returned `id=1`.
- Ran `pearson-claude-token-sync` and verified the remote Pearson WSL token
  hash matched the repaired local B bearer (`a3ad80d7`).
- Cleanly exited and resumed the affected Pearson project panes so they
  re-read the repaired credentials:
  - Ecom resumed `ac4ba3e7-fe6b-452b-9569-44d8b3a0f1f8`.
  - Clever resumed `f9fedeea-bd8c-411e-98f1-41819c602d27`.
- Left Root and Gauge untouched because they were not showing the old
  credential failure during verification.

### Systematic recovery path

Use this order when a future zellij/DC/Pearson tab reports
`active account changed`, `Please run /login`, or a long-window upstream
retry-after:

1. Read `/__throttle/health` first. If `inflight=0`, `queued=0`, and central is
   up, do not tune queue knobs.
2. Run `claude-account-pick status`. If A/B show the same identity id, do not
   cycle tabs yet; account switching is cosmetic until the credential files are
   repaired.
3. Back up the affected `~/.claude*` credential/profile files.
4. Reject dead backups by probing refresh-token validity before restore. An
   `invalid_grant` backup is not a recovery source.
5. Re-authenticate the collapsed profile in its own `CLAUDE_CONFIG_DIR` with a
   dedicated browser profile. Do not run interactive `/login` inside each
   project pane.
6. Verify `claude-account-pick status` shows distinct identities.
7. Run the project-specific token sync helper when the work happens in remote
   WSL/tmux/zellij state (`pearson-claude-token-sync` for Pearson).
8. For each affected Claude pane, prefer `/exit`, capture the resume id, launch
   the wrapper with `claude --resume <id>`, and send `continue`.

Falsifier for this diagnosis: if A/B identities are distinct and the active
credential hash matches the pane's bearer, then the failure is not credential
collapse; inspect upstream `Retry-After`, utilization windows, and the per-bearer
limiter instead.

---

## 24/06/2026 - DeliCasa tabs hit credential-swap 401

### What was wrong

After the local proxy/Nix activation gap was closed, account A (`4400c70e`)
remained under a long `Retry-After` until `26/06/2026 07:00 -03`. The active
credential was promoted to bearer `ae018561`, which made stale long-lived
Claude Code TUIs correctly receive:

    Please run /login · API Error: 401 throttle-proxy: active account changed; re-read credentials

The DeliCasa zellij session had been restored with 7 Claude panes, but those
panes still held the old in-memory token. The existing `claude-tabs-cycle`
automation did not recover them because:

- its API-error regex only matched banners that started directly with
  `API Error`, not the new `Please run /login · API Error` prefix.
- its scan loop incremented before dumping panes, so `terminal_0` was skipped.

Manual `/login` was the wrong recovery path: it opened the interactive account
login menu. The safe path was to cleanly `/exit`, capture the printed resume
token, relaunch via the wrapper with `claude --resume <uuid>`, and send
`continue`.

### What was repaired

- Promoted the standby credential with `claude-account-promote --now` at
  `24/06/2026 14:50 -03`. Active became `ae018561`; parked A stayed
  `4400c70e`.
- Recovered every DeliCasa Claude pane by clean exit/resume, preserving the
  prior sessions:
  - Root: `24ea66f0-dd3a-4ff4-b884-722041111fb7`
  - NextClient: `4a48a827-75f3-489a-a850-d0d127fb2f57`
  - BridgeServer: `9274bd67-3abf-4013-9d72-1d3e39096f80`
  - Wire: `962ea2f7-e503-49a5-85eb-90881aece030`
  - ESP32: `24877a0a-b38a-4e02-9b33-d7dbb834b0c5`
  - PiOrch: `e2bfc40e-7775-4047-9cde-554407317192`
  - PiDash: `0bbadd0e-d46a-4a90-8432-7c326d4ee8e8`
- Merged `phsb5321/NixOS#1032` at `24/06/2026 16:38 -03`
  (`ad4b5cbc45d3f911cb938120f7e27b1bdc2122df`):
  - `claude-tabs-nudge` and `claude-tabs-cycle` now classify
    `Please run /login · API Error ...` as a stuck credential-swap banner.
  - `claude-tabs-cycle` starts scanning at `terminal_0`.
  - regression coverage locks both cases.
- Activated desktop at `24/06/2026 16:40 -03`:
  `/nix/store/dl7h53pmfh5c2a51wzm0bv727wv7mjlk-nixos-system-desktop-26.11.20260616.3e41b24`.

### Final verification

- `python3 modules/home/claude-tabs-cycle.test.py` -> all cases pass.
- `git diff --check` -> clean.
- `nix eval .#nixosConfigurations.desktop.config.system.build.toplevel.drvPath --raw`
  -> succeeded.
- `nh os switch .` built and activated successfully; both
  `claude-tabs-cycle` and `claude-tabs-nudge` built in the activation graph.
- `CYCLE_SESSION=DeliCasa claude-tabs-cycle --dry-run` -> `scanned=6
  stuck-cyclable=0`, with the only visible stuck-banner pane skipped because it
  had active work. No keystrokes were sent.
- DeliCasa pane readback showed all 7 project tabs running `claude --resume`
  with the tokens listed above.
- Local proxy readback:
  - `anthropic-throttle-proxy.service`, `.socket`, and keepalive timer all
    `active`.
  - `/__throttle/health`: `queue=fair`, `central=up`, `inflight=0`, `queued=0`,
    `upstream_retries=0`.
  - active bearer `ae018561`: `served=504`, no `retry_after`, `util_5h=0.10`,
    `util_7d=0.97`, limiter max `12`.
  - parked bearer `4400c70e`: no served traffic, retry-after until
    `26/06/2026 07:00 -03`.

Rollback for the NixOS helper fix is a normal revert PR:
`git revert ad4b5cbc45d3f911cb938120f7e27b1bdc2122df`.

---

## 24/06/2026 - stale A-account tabs still rate-limited

### What was wrong

Pedro asked to check previous-session work and explain why Claude Code tabs
were still rate-limited.

Live proxy state was healthy, not queue-jammed:
`/__throttle/health` returned `inflight=0`, `queued=0`, `queue_mode=fair`,
`central_status=up`, and `upstream_egress_ok=true`. The live problem was
account state:

- then-current A credential hash (`~/.claude/.credentials.json`) was `ac454a4f`.
  That bearer was exhausted and held behind a long `Retry-After` until the
  7-day reset at `26/06/2026 07:00 -03`.
- then-current B credential hash (`~/.claude-b/.credentials.json`) was `324eadec`.
  That bearer was usable but already in `allowed_warning` with
  `util_7d=0.91` and reset at `29/06/2026 05:00 -03`.
- 39 `.claude-wrapped` processes were still running. Tabs still holding the A
  token in memory kept hitting bearer `ac454a4f`, so the proxy correctly
  returned the long-window fast-fail 429.

The previous proxy-side fix was merged in this repo as PR #59
(`741d1be`, 401 nudge for stale tabs), but the desktop service was still
running an older Nix package:

- systemd `ExecStart` pointed at
  `/nix/store/0zncmhx20a1mg3ckrfay1vxar3m5q1id-anthropic-throttle-proxy-0.1.0`.
- that store path's `_retry_after_fast_fail_response()` only returned a local
  429; it had no `credential-nudge` / `THROTTLE_ACTIVE_CRED_PATH` code.
- the live service environment had `THROTTLE_ACCOUNT_CRED_PATHS=...` but did
  not set `THROTTLE_ACTIVE_CRED_PATH`.

So the root cause was an integration/activation gap: source `main` contained
the #59 nudge, but the host package and service env did not. The limiter was
not malfunctioning.

### What was repaired

- Took over the previous NixOS integration PR:
  `phsb5321/NixOS#1025` ("single-active-account credential failover").
- Verified before merge:
  - `python3 modules/home/claude-account-promote.test.py` -> 9/9 passed.
  - `git diff --check` -> clean.
  - `nix eval .#nixosConfigurations.desktop.config.system.build.toplevel.drvPath --raw` -> succeeded.
  - `nix eval .#nixosConfigurations.server.config.system.build.toplevel.drvPath --raw` -> succeeded.
  - GitHub PR state was open, non-draft, mergeable/clean; GitGuardian passed;
    no review comments.
- Squash-merged NixOS PR #1025 at `24/06/2026 14:00 -03` as
  `685c06a5085a5fdcb97c9d58b30ed411586d0df7`, deleted the remote branch, and
  removed the local PR worktree/branch.

### Final closure

NixOS main first had only the integration, but the desktop host had not been
activated. The first readback still showed:

- `ExecStart=/nix/store/0zncmhx20a1mg3ckrfay1vxar3m5q1id-...`
- no `THROTTLE_ACTIVE_CRED_PATH` in the service environment
- local health still showing A `ac454a4f` under `Retry-After` and B
  `324eadec` usable.

Later readback on 24/06/2026 showed the on-disk credential files had rotated
again while stale bearer IDs remained in the live proxy process:

- `~/.claude/.credentials.json` hashed to `4400c70e`, expiring at
  `24/06/2026 22:00 -03`.
- `~/.claude-b/.credentials.json` hashed to `ae018561`, expiring at
  `24/06/2026 22:00 -03`.
- `/__throttle/health` still showed old bearer `ac454a4f` under the long
  `Retry-After`, old bearer `324eadec` in `allowed_warning`, and current B
  bearer `ae018561` serving requests.
- systemd still reported the old store path and no `THROTTLE_ACTIVE_CRED_PATH`.

Pedro then asked to solve everything. Follow-up work closed the activation and
package-pin gap:

- Merged `phsb5321/NixOS#1030` at `24/06/2026 14:19 -03`:
  `fable-tag` now requires `fable-tag <dir> <model>` and no malformed default
  remains in generated `~/.zshrc`.
- Activated desktop once at `24/06/2026 14:34 -03`, which added
  `THROTTLE_ACTIVE_CRED_PATH=/home/notroot/.claude/.credentials.json` but still
  left the service on the old PR #55 proxy package.
- Merged `phsb5321/NixOS#1031` at `24/06/2026 14:42 -03`: bumped
  `pkgs/anthropic-throttle-proxy` from PR #55 (`2ad7fc8`) to PR #59
  (`741d1be9d0baa54c536a16304e0eaaec50412d98`).
- Activated desktop again at `24/06/2026 14:44 -03`.
- Final systemd readback showed the live service restarted at
  `24/06/2026 14:44:57 -03` with:
  - `ExecStart=/nix/store/296fif3ij846sv741vf5h8ji0pgmxgkg-anthropic-throttle-proxy-0.1.0/bin/anthropic-throttle-proxy`
  - `THROTTLE_ACTIVE_CRED_PATH=/home/notroot/.claude/.credentials.json`
  - `ActiveState=active`, `SubState=running`
- Final `/__throttle/health` showed `inflight=0`, `queued=0`,
  `upstream_egress_ok=true`, `central_status=up`, and a clean in-memory
  `bearers={}` state after the service restart.
- Verified the live store path contains `_active_account_bearer`,
  `ACTIVE_CRED_PATH`, and `credential-nudge`.

### Extra discrepancy found

The 23/06 entry says `fable-tag` was repaired to require an explicit model.
That was not durable. On 24/06, live `~/.zshrc` and the NixOS source still had
an implicit malformed default:

    local dir="${1:-$PWD}" model="${2:-claude-fable-5[1m]}"

This was NOT the current rate-limit cause: the repo-local
`.claude/settings.local.json` was absent, and `~/.claude/settings.json` plus
`~/.claude/settings.json.tmp` had no `model` key. This discrepancy is now
closed by NixOS PR #1030 and the
`24/06/2026 14:34 -03` desktop activation; generated `~/.zshrc` contains the
`usage: fable-tag <dir> <model>` guard and no `claude-fable-5[1m` match.

---

## 23/06/2026 - desktop Claude Code tabs all rate-limited

### What was wrong

Pedro reported that no Claude Code tab on desktop was working and asked whether
the previous session `c430b7c3-a6ac-4293-bf40-590a663a3ebe` had broken
anything.

Live proxy state was healthy: `/__throttle/health` returned
`inflight=0`, `queued=0`, `queue_mode=fair`, `central_status=up`, and
`upstream_egress_ok=true`; `/` returned `anthropic-throttle-proxy`. The
service was also active under systemd with no transient override drop-in.

There were two independent local problems:

1. `~/.zshrc` had a `fable-tag` helper with an implicit malformed default model
   (`claude-fable-5[1m`, later accidentally reduced to `claude-fable-5]` during
   cleanup). This could write bad `.claude/settings.local.json` files and force
   Claude Code away from its default model selection.
2. The repo root had an untracked `.claude/settings.local.json` containing only
   `"model": "claude-fable-5[1m]"`, so Claude Code launched from this repo read
   a bad project-local model override.

The previous session was also manipulating Claude account profiles. Its task
list included logging `~/.claude-b` into `pedrobalbino@pm.me` and a pending
task to restore `~/.claude` to `pedrobalbino@proton.me`. At repair time,
`~/.claude/.claude.json` already identified A as `pedrobalbino@proton.me`.

### What was repaired

- Changed `fable-tag` in `~/.zshrc` so it has no implicit model default. It now
  requires an explicit model argument before writing any `model` key.
- Removed the repo-local untracked `.claude/settings.local.json`, letting
  Claude Code use its default model from this repo.
- Removed the stale malformed `model` key from `~/.claude/settings.json.tmp`.
- Terminated 40 stale `.claude-wrapped` processes with `TERM` so new tabs load
  the repaired settings and current credential files. No `KILL` was needed.

### Remaining hard limit

The proxy still reported real upstream quota pressure after cleanup. Three
bearer hashes were held behind `Retry-After` until the 7-day reset at
`26/06/2026 07:00 -03`; one had `util_7d=1.0`. The current A/B credential
hashes did not match those blocked hashes, so fresh Claude tabs should not
inherit the old in-memory exhausted tokens. If new tabs still fail, capture a
fresh `/__throttle/health` and map the active credential hash before changing
proxy behavior.

### Verified state after cleanup

- No `.claude-wrapped` processes remained.
- `~/.claude/settings.json` and `~/.claude/settings.json.tmp` both returned
  `has("model") == false`.
- The repo-local `.claude/settings.local.json` no longer existed.
- `~/.claude/.claude.json` reported `pedrobalbino@proton.me`.
- `/__throttle/health` still returned `inflight=0`, `queued=0`,
  `central_status=up`, and `upstream_egress_ok=true`.

---

## 06/06/2026 — central nginx upstream stale after Dokku-host reboot

### What was wrong

Pedro's prior Claude Code session crashed with:

    API Error: The socket connection was closed unexpectedly. This is often a
    network or firewall issue. ...
    500 ... anthropic-throttle.home301server.com.br

Local proxy stayed healthy. The failure was central-only, between Dokku's
nginx vhost and the `anthropic-throttle.web.1` container.

**Causality status: ◐ partially verified, NOT proven.**
`dokku proxy:build-config anthropic-throttle` repaired the central tier,
but the pre-recovery `/home/dokku/anthropic-throttle/nginx.conf` was
never captured. The stale-upstream-IP theory below is the *most likely
mechanism*, not a proven root cause. The missing falsifier is the
pre-recovery upstream IP (or nginx error-log lines showing connection
attempts against an old container IP). See "Required wording change"
note at the bottom of this section.

Most likely mechanism, in five steps. Each line marks whether the
evidence was captured pre-recovery (P), post-recovery (POST), or is
inference (I) not measured here:

1. (P) Pedro ran `sudo apt-get upgrade -y` on the Dokku host at 19:11:29
   on 2026-06-06 (`/var/log/apt/history.log`). The upgrade bumped
   `docker-ce` / `docker-ce-cli` / `docker-ce-rootless-extras`
   29.5.2 → 29.5.3, `dokku` 0.38.10 → 0.38.17, plus `cloud-init`,
   `apparmor`, `firmware-sof-signed`, `trivy`. (P) `dockerd` restarted
   at 19:11:48 (host journal). (I) "First container-IP-shuffle event"
   is inferred from Docker IPAM behavior — not measured here, because
   the pre-event container IP was not captured.

2. (P) At 19:17:49 Pedro ran `sudo systemctl reboot` (`sudo[2284478]:
   notroot : COMMAND=/usr/bin/systemctl reboot`, followed by
   `systemd-logind: System is rebooting`). Orderly shutdown, not a
   crash. The pre-staged kernel (5.15 → 5.15.124, installed Jun 3 06:00
   by unattended-upgrades) finally activated. (I) "Second IP-shuffle
   event" — again inferred, not measured.

3. (POST) `anthropic-throttle.web.1` came up on bridge IP `10.0.0.24`
   (`dokku ps:report` after recovery). Docker's default IPAM is
   sequential, not stable across daemon restarts or host reboots, so
   the IP *may* have been different before either event — but the
   pre-event IP was never captured, so this remains hypothesis.

4. (I, unproven) Dokku does NOT auto-regenerate per-app
   `/home/dokku/<app>/nginx.conf` on host reboot, on dockerd restart,
   or on container respawn. The hypothesis is that the vhost kept
   serving a stale `upstream` directive against an unreachable old IP.
   This is the unproven step: the pre-recovery vhost was not preserved
   before running `proxy:build-config`, which destroyed the forensic
   artifact.

5. (I) nginx returned 502 / `connection closed by upstream` to the
   local proxy. Local proxy's central forward failed; PR #23
   (25/05/2026 fix, merged as `78c2c43d`) promoted local admission to
   `fair` and fell back to direct upstream. Pedro saw a user-visible
   `500 ... anthropic-throttle.home301server.com.br`, which means
   *some* failures escaped the queue + retry path. Whether that is
   expected (e.g. the central response had already started streaming
   when nginx returned 502, so retry was unsafe) or a remaining
   fallback bug is an open question — see Follow-up #5.

Plausible alternatives that are NOT ruled out and would also explain
the symptom:

- The Dokku 0.38.10 → 0.38.17 upgrade itself changed proxy-config
  behavior, failed a postinst hook, or left nginx serving an older
  config until `proxy:build-config` repaired it. The doc does not
  prove this is *not* what happened.
- `nginx -t` could have failed silently after the upgrade. The cert
  mismatch on the same vhost (Follow-up #1) is independent evidence
  that nginx-side state on this host is already in a confused state,
  which makes silent reload failure not implausible.
- `dokku-watchdog` or app-level healthcheck flap could have
  temporarily marked the container unhealthy and routed nginx away
  from it. We did not pull watchdog / Docker-event / healthcheck logs
  before recovery.
- This may not be app-specific. We did not enumerate other Dokku
  apps' `/home/dokku/<app>/nginx.conf` vs live `dokku ps:report` IPs,
  so the scope ("only this app drifted" vs "many drifted") is
  unverified.

### The first hypothesis was wrong

Initial framing: "unattended-upgrades nightly run rebooted the host
because a kernel upgrade landed."

Falsified by:

- `last reboot | head -3` → `Sat Jun 6 19:19  still running`, prev boot
  `Jun 4 04:01 → Jun 6 19:17`. Single recent boot, matched the
  failure window.
- `last shutdown | head -3` → `Sat Jun 6 19:17 - 19:19 (00:01)`. Orderly
  1-minute shutdown.
- Journal at 19:17:49 → `sudo[2284478]: notroot :
  COMMAND=/usr/bin/systemctl reboot`. Pedro triggered it manually.
- `/var/log/unattended-upgrades/unattended-upgrades.log` had no 19:1x
  entries on 2026-06-06; last cycles were 06:20 and 09:35. The kernel
  was pre-installed at 06:00 on Jun 3 but only activated by Pedro's
  manual reboot.

Actual trigger: Pedro's interactive `apt-get upgrade -y` (which cycled
dockerd as a side effect of the docker-ce 29.5.2 → 29.5.3 bump),
followed by `systemctl reboot`. Whether *those host events* caused
container IP drift, and whether IP drift caused the bad vhost, is the
unproven step (see "Causality status" above).

### The fix that shipped

Recovery was a single Dokku command on the host:

    dokku proxy:build-config anthropic-throttle

This regenerated `/home/dokku/anthropic-throttle/nginx.conf` with
`upstream anthropic-throttle-8765 { server 10.0.0.24:8765; }`. The
command's own output reported nginx reload success, but no separate
`nginx -t` / `systemctl reload nginx` / `journalctl -u nginx` /
`nginx -T` was captured to independently prove workers picked up the
new config — see Mistake #6.

Verified after recovery (all POST-recovery; the matching pre-recovery
captures do not exist):

- `curl http://anthropic-throttle.home301server.com.br/__throttle/health`
  from the Dokku host (NOT from the local-proxy host through DNS) →
  `queue_mode=fair`, `served` counter climbing, no 502.
- Local proxy `/__throttle/health` (loopback `127.0.0.1:8765`) →
  `central_status=up`, `via=null` (central path active), `served`
  resuming. This proves the local proxy's *own* central probe
  recovered; it does not independently re-test the same DNS + nginx
  vhost + container path the failing requests took. See Mistake #2.
- `/home/dokku/anthropic-throttle/nginx.conf` mtime updated to 19:43,
  post-recovery upstream IP matched `dokku ps:report` container IP.
  The pre-recovery upstream IP and mtime were not captured, so we
  cannot show the file actually changed in a way that would explain
  the symptom.

Container at recovery (POST):

- CID `fa93c1b363c`, IP `10.0.0.24`, FinishedAt 19:19:47 → StartedAt
  19:19:51, RestartCount=0, ExitCode=0, OOMKilled=false. Reboot
  recovery, not a deliberate dokku op.

No code or Nix pin changed. This was a host-side data-plane recovery,
not a proxy-software bug.

### Open follow-ups

These are separate from the inline incident work above; tracked here
so the next session does not re-discover them. Numbered for
cross-reference from Mistakes and the workflow section.

1. **TLS regression on the external HTTPS endpoint — incident-relevant,
   not cosmetic.** `curl -v
   https://anthropic-throttle.home301server.com.br/__throttle/health`
   serves the `ai-docs.home301server.com.br` certificate — SAN
   mismatch. Local proxy uses `THROTTLE_CENTRAL_URL=http://...` so the
   workflow is unaffected, but the public HTTPS path is broken AND it
   means the nginx server-name → cert binding for this vhost is
   already in a confused state. That makes the silent-stale-config /
   silent-reload-failure alternative (see "What was wrong" plausible
   alternatives) more credible, not less. Investigate `dokku
   letsencrypt:list` together with this incident's nginx state, not
   as a separate cosmetic bug.

2. **Systemic guard against this exact failure mode.** The host
   already runs `/usr/local/bin/authelia-upstream-sync.sh` every
   minute via cron to resync that app's upstream IP after container
   drift. The same pattern generalizes: a small cron that diffs
   `dokku ps:report <app>` container IP against the upstream in
   `/home/dokku/<app>/nginx.conf` and runs `dokku proxy:build-config
   <app>` on mismatch — for *every* Dokku app, not just this one.
   Alternative: pin container IPs via Docker IPAM. Either prevents
   the next reboot or `apt upgrade docker-ce` from silently breaking
   any app on this host.

3. **Post-upgrade / post-reboot Dokku app proxy consistency check.**
   Add a one-shot script that runs after every `apt upgrade` and on
   every boot: enumerate `/home/dokku/*/nginx.conf`, compare each
   `upstream` IP to the live `dokku ps:report` container IP, and
   either alert or auto-`proxy:build-config` on mismatch. Closes the
   same gap as #2 but at the boundary, not on a polling cron.

4. **Local proxy log/metric distinguishing central failure modes.**
   The local proxy currently treats any central forward failure as
   one bucket. Distinguish: HTTP 502 from nginx, connection refused
   (no nginx), socket close mid-stream, DNS failure, timeout. The
   user-visible 500 in this incident does not tell us which one
   Pedro hit, which makes it impossible to decide whether the
   fallback behaved as designed. Add separate counters
   `central_failure_total{kind="..."}`.

5. **Regression test: does central HTTP 502 trigger direct fallback?**
   Open question from "What was wrong" #5 / Mistake #4. Add a test
   that stands up a fake central returning 502, runs a request
   through the local proxy in central-configured mode, and asserts
   either (a) the request succeeded via direct fallback, or (b) the
   request failed with a documented status. Either way the answer
   becomes contract, not folklore.

6. **Alert for central returning 502 from nginx.** Prometheus rule
   on the local proxy or the central tier (whichever sees nginx 502
   on the central forward) so the next occurrence pages instead of
   surfacing as a session crash. Pair with a runbook entry pointing
   at this handoff.

7. **Audit ALL Dokku apps for stale upstream risk now, not after the
   next incident.** Run the diff in #3 once today across the host;
   find any other apps whose vhost upstream IP doesn't match the
   live container, and rebuild them. The reboot at 19:17 may have
   left more apps in this state — we did not check. Plain HTTP
   smoke tests of externally important apps would be a faster
   first pass than enumerating files.

8. **Enable `dokku events:list` event logging.** Currently disabled
   on this host, so the Dokku-side audit trail beyond the journal
   is gone. Enable it as part of systemic hardening; this incident
   would have been easier to triage with a Dokku-side event timeline.

### Mistakes made during the session

Do not repeat these. Numbered for cross-reference.

1. **The biggest miss: failed to preserve pre-recovery evidence
   before running `dokku proxy:build-config`.** The pre-recovery
   `/home/dokku/anthropic-throttle/nginx.conf` (with the suspected
   stale upstream IP) was never copied or printed. The fix command
   destroyed the most important forensic artifact in this incident.
   Also missed before recovery: `nginx -T | grep -A3
   anthropic-throttle`, `/var/log/nginx/error.log` lines for the
   failing hostname, `docker events --since '30m ago'`, `dokku logs
   anthropic-throttle`, full `docker inspect anthropic-throttle.web.1`.
   After this, root cause could only be inferred, not proven.

2. **Did not verify central health from the failing path.**
   Re-checked health by curling
   `http://anthropic-throttle.home301server.com.br/__throttle/health`
   from the Dokku host itself, and by hitting `127.0.0.1:8765` on the
   local proxy. Neither retraces the actual failing path: from the
   local-proxy host, through the configured `THROTTLE_CENTRAL_URL`,
   through DNS, through nginx vhost, to the Dokku container. That
   end-to-end check was skipped.

3. **Did not check blast radius across other Dokku apps.** If host
   reboot or `apt upgrade docker-ce` causes upstream-IP drift, every
   per-app vhost on this host is at risk. The session never
   enumerated `/home/dokku/*/nginx.conf` upstreams against live
   `dokku ps:report` IPs. Other apps may still be drifting silently.

4. **Did not rule out alternative causes.** The four candidates
   below were left uneliminated, any of which would have produced
   the same symptom:
   - The Dokku 0.38.10 → 0.38.17 upgrade itself (config-handler
     behavior change, failed postinst hook, nginx-config-test
     failure after the upgrade).
   - `dokku-watchdog` or app-level healthcheck flap routing nginx
     away from a temporarily-unhealthy container.
   - A stale `nginx -t` failure that prevented reload and left
     workers on an old config, made more plausible by the cert
     mismatch on the same vhost (Follow-up #1).
   - A central-side throttle-proxy crash that recovered before
     evidence was captured.

   Pulling `dokku logs anthropic-throttle`, watchdog logs, Docker
   events, and `journalctl -u nginx` *before* recovery would have
   ruled these in or out.

5. **Did not run `dokku events:list` early.** It would have been
   the fastest Dokku-side audit signal. The fact that it returns
   "Events logger disabled" on this host is itself a finding —
   that should have been caught in this session, not left as a
   passive follow-up.

6. **Did not independently verify nginx reload after
   `proxy:build-config`.** The fix command's own stdout reported
   reload success; no separate `nginx -t`, `systemctl status nginx`,
   `journalctl -u nginx --since`, or `nginx -T` was run to prove
   workers picked up the new config. Given the cert mismatch on the
   same vhost (Follow-up #1), nginx state on this host is already
   suspect and silent reload failure is not impossible.

7. **Treated `apt upgrade` and `unattended-upgrades` as synonyms.**
   They are different operators (manual interactive vs nightly
   background), with different evidence trails. Always check
   `/var/log/apt/history.log` AND
   `/var/log/unattended-upgrades/unattended-upgrades.log`, not just
   one.

8. **First search for stale nginx config looked under
   `/etc/nginx/conf.d/`.** Since Dokku 0.30 the per-app vhost lives
   at `/home/dokku/<app>/nginx.conf`, with overrides under
   `/home/dokku/<app>/nginx.conf.d/`. Verify `dokku version` (0.38.17
   here) before locating files.

9. **Initial hypothesis named "kernel auto-reboot" without first
   running `last reboot` and `last shutdown`.** The trigger evidence
   was one journal line away. Capture audit trails before forming
   hypotheses.

10. **Did not initially separate "container respawned" (`FinishedAt
    19:19:47 → StartedAt 19:19:51`) from "host rebooted" (boot ID
    flip at 19:17:56 → 19:19:42).** The four-second container gap
    looked like a deliberate `dokku ps:restart`; it was actually
    Docker-daemon recovery during the reboot. `last reboot` plus
    boot ID inspection disambiguates.

### How Claude should have solved it

The correct workflow when central is unhealthy and local is fine:

1. **Capture host-side audit trail first, before forming hypotheses:**
   - `last reboot | head -3`
   - `last shutdown | head -3`
   - `journalctl --since '2 hours ago' | grep -E 'sudo|systemd-logind: System is'`
   - `/var/log/apt/history.log` last block (manual upgrades)
   - `/var/log/unattended-upgrades/unattended-upgrades.log` last cycle
   - `dokku version` (locates per-app nginx.conf path)
   - `dokku events:list` (and surface "logger disabled" as a
     finding, not a passive follow-up)

2. **State hypothesis in one sentence with an explicit falsifier.**
   Example: "central nginx upstream is stale because the container IP
   changed after a host event; falsifier: if `dokku ps:report` IP
   matches the `upstream` line in `/home/dokku/<app>/nginx.conf`, the
   IP-drift theory is wrong."

3. **Preserve pre-fix evidence — this is the step the 06/06/2026
   session skipped.** Before any recovery command, copy or print:
   - `/home/dokku/<app>/nginx.conf` (the suspected stale file —
     `cp` it aside, do NOT just `cat` it once)
   - `nginx -T 2>/dev/null | grep -A 3 -E "<app>|<hostname>"`
   - `/var/log/nginx/error.log` lines for the failing hostname
     (`grep <hostname> /var/log/nginx/error.log | tail -200`)
   - `journalctl -u nginx --since '2 hours ago'`
   - `docker events --since '30m ago' --until 'now'` (post-hoc
     replay; otherwise capture live next time)
   - `dokku logs <app> --tail`
   - `docker inspect <app>.web.1` full output
   - `dokku ps:report <app>` (current container IP)
   - For multi-network apps, every IP from `docker inspect`'s
     `NetworkSettings.Networks` map, not just the bridge IP

4. **Prove or falsify the upstream-IP path against current state:**
   - Diff captured `dokku ps:report` IP against `grep -A1 upstream
     /home/dokku/<app>/nginx.conf`.
   - Mismatch → confirms IP drift, the most likely mechanism.
   - Match → IP drift falsified; reach for the other candidates
     (Mistake #4 alternatives: Dokku upgrade behavior, watchdog
     flap, failed nginx reload, cert/server-name binding bug,
     central-proxy crash).

5. **Apply minimal reversible recovery:**
   - `dokku proxy:build-config <app>`

6. **Independently verify nginx reload, NOT just the Dokku
   command's stdout:**
   - `nginx -t` (must print `syntax is ok` / `test is successful`)
   - `journalctl -u nginx --since <timestamp>` (look for reload
     line + zero errors)
   - `nginx -T 2>/dev/null | grep -A 3 <app>` confirms live config
     matches the new file
   - File mtime alone (as in this incident) is NOT sufficient —
     workers may not have picked it up.

7. **Re-verify the failing path end-to-end, not loopback:**
   - `curl -v $THROTTLE_CENTRAL_URL/__throttle/health` from the
     local-proxy host, through DNS + nginx vhost + container.
     Loopback `127.0.0.1:8765` and Dokku-host-local curls do NOT
     retrace the failing path.
   - Local proxy `central_status=up` and `via=null` are necessary
     but not sufficient.

8. **Audit blast radius before declaring done:**
   - Enumerate `/home/dokku/*/nginx.conf` upstream IPs vs `dokku
     ps:report` per app on the same host. If multiple apps drifted,
     this is a host-wide event, not an app-specific one.

9. **Document this incident, run Codex adversarial review per the
   section below, address findings, ship the doc PR, then file
   the open follow-ups as separate issues.**

### Required wording change (carried over from Codex review)

Per Codex's adversarial review of an earlier draft of this section:
the root-cause language was overclaiming. The honest framing —
preserved here so a future session does not regress it — is:

> Most likely mechanism: host upgrade/reboot caused container or
> proxy state drift, and `dokku proxy:build-config anthropic-throttle`
> repaired nginx-to-container routing. We did not preserve the
> pre-recovery nginx upstream, so stale-IP causality is not proven.
> The missing falsifier is the old upstream IP or nginx error logs
> showing connection attempts to an old container IP.

Mark every future incident's evidence with (P) / (POST) / (I) so the
proven-vs-inferred boundary stays visible.

---

## 25/05/2026 — local fallback bypassing local queue

### What was wrong

Pedro reported Claude Code requests sitting at "typed but no spinner" and
server-side rate limiting while the local proxy was configured as
`THROTTLE_QUEUE_MODE=off`.

The live system had two separate issues:

1. Central throttle was doing real queueing.
   `http://anthropic-throttle.home301server.com.br/__throttle/health` showed
   `queue_mode=fair`, active queued work, and high 7-day utilization. Some delay
   was expected because the central tier intentionally holds requests before
   upstream response start.

2. Local fallback was unsafe when central flapped.
   The desktop local tier was configured with `THROTTLE_QUEUE_MODE=off` because
   central owns normal fleet-wide queueing. When central went down or a central
   forward failed, the local tier retried direct upstream while still in
   pass-through mode. That meant a central flap could become an unqueued direct
   firehose against Anthropic.

The fix that shipped:

- `yolo-labz/anthropic-throttle-proxy#23`
  - merged as `78c2c43d48228ddd09e543a2219019209ffbd17e`
  - promotes local admission to `fair` when central is configured but routing is
    direct fallback
  - serializes emergency direct retries after a central forward failure
  - keeps promoted fair limiters from being downgraded back to `off`
- `phsb5321/NixOS#703`
  - merged as `6810250dcef68637d047ac6544911b4401deac30`
  - bumps the Nix package pin to the fixed upstream commit
- Host activation completed with:
  - `/nix/store/055538y8i9r96lycpq5mx2pc6yk6mhnv-nixos-system-desktop-26.05.20260523.3d8f0f3`
  - active user unit points at `/nix/store/ffl8ngpi6y4f49vps94r6w7w89nfsa59-anthropic-throttle-proxy-0.1.0`

### Mistakes made during the session

Do not repeat these.

- The first response treated sandbox restrictions as a stopping point too early.
  The right move was to keep looking for a route: GitHub API/`gh`, `/tmp`
  worktree/clone, Nix build with a writable cache, and host activation from a
  clean checkout.
- A temporary workspace proxy was started while systemd was also trying to own
  `127.0.0.1:8765`. That created the bind collision that made the fixed systemd
  unit restart-loop. If you start an emergency process, record its PID and kill
  it before handing control back to systemd.
- The first NixOS PR upload accidentally emptied
  `pkgs/anthropic-throttle-proxy/default.nix` by using Perl in-place editing
  with shell redirection. Always parse/check generated Nix files before upload.
- Local dirty main was left behind from the emergency code edit. Future doc or
  follow-up work must use a dedicated worktree first.
- Early checks looked at the symptom, but the important proof came from
  combining health JSON, journal lines, and the exact fallback path in code.
  Similar-looking rate-limit incidents are not enough evidence.

### How Claude should have solved it

The correct incident workflow is:

1. Capture live state first:
   - `curl http://127.0.0.1:8765/__throttle/health | jq`
   - central health at `THROTTLE_CENTRAL_URL`
   - `journalctl --user -u anthropic-throttle-proxy -n 200 --no-pager`
   - current `ANTHROPIC_BASE_URL`, service `ExecStart`, and Nix store path
2. State the hypothesis in one sentence.
   Example: "Local central fallback is bypassing the local queue in `off` mode,
   so central flaps create direct unqueued Anthropic bursts."
3. Prove or falsify that hypothesis against the exact code path:
   - `pick_target()`
   - `_get_bearer_limiter()`
   - `_retry_direct_once()`
   - queue mode transitions in `FairBearerLimiter`
4. Apply the smallest live mitigation that is reversible:
   - `/ui/config` or direct POST to set `min_dispatch_gap_ms`
   - verify it persisted in
     `~/.local/state/anthropic-throttle-proxy/overrides.json`
5. Patch the repo in a feature worktree and test:
   - targeted forwarding tests
   - full pytest
   - ruff
6. Publish and merge the upstream proxy fix.
7. Bump the NixOS package pin and fixed-output hash.
8. Wait for NixOS CI, including host eval.
9. Activate the host from a clean checkout, not the dirty repo checkout.
10. Verify the actual runtime:
    - user unit `ExecStart` points to the fixed store path
    - `curl http://127.0.0.1:8765/__throttle/health` responds
    - journal shows the fixed store path and no bind failure

---

## Required adversarial review (applies to all incidents)

Before merging a throttle behavior change, ask Codex for adversarial review.
The review prompt must include:

- the user-visible symptom
- the one-sentence hypothesis
- live health/journal evidence, marked (P) pre-recovery, (POST)
  post-recovery, or (I) inference
- the exact code diff (or recovery action, for host-side incidents)
- test results
- the deployment/pin/activation plan
- for host-side incidents: pre-recovery captures of the suspected
  stale config (e.g. `/home/dokku/<app>/nginx.conf`), nginx error
  logs for the failing hostname, `docker events`, `dokku logs`,
  and the relevant `journalctl -u nginx` window

Codex must be asked to look specifically for:

- unproven causality, especially "correlation dressed up as root cause"
- fallback paths that still bypass local queueing
- limiter mode transitions that can strand queued futures
- central/local behavior mismatches
- Nix pin or fixed-output hash mistakes
- host activation gaps where merged code is not actually running
- for host-side incidents: container-IP drift, stale per-app nginx
  vhost, mismatch between `dokku ps:report` and
  `/home/dokku/<app>/nginx.conf`, silent `nginx -t` / reload
  failures, and blast radius across other apps on the same host
- whether pre-recovery forensic artifacts were preserved before the
  fix command was run

Do not merge or declare the incident solved until the adversarial findings are
addressed or explicitly documented with evidence.

---

## 29/06/2026 — short Retry-After fast-fail incident

### Symptom

Claude tabs surfaced:

```text
API Error: Server is temporarily limiting requests (not your usage limit) · upstream retry-after window is active; failing fast instead of holding the local gateway request
```

### Verified cause

Anthropic returned short `Retry-After: 30` throttles for the active OAuth
bearer while the proxy default held only `15s`. The proxy therefore fast-failed
requests that should have been held and retried. Official docs say earlier
retries before `retry-after` expires will fail, and Claude Code's own error
guide classifies "Server is temporarily limiting requests" as a short-lived
throttle unrelated to plan quota.

Live mitigation applied at 29/06/2026 17:32 -03:

```bash
POST /ui/config key=max_hold_retry_after_s value=60
POST /ui/config key=max_concurrent value=5
POST /ui/config key=central_local_max_concurrent value=5
POST /ui/config key=aimd_initial_concurrent value=3
POST /ui/config key=aimd_backoff_s value=30
POST /ui/config key=aimd_ramp_after value=6
```

This fixed the visible fast-fail wording, but did not fully solve rate
pushback. Central Dokku logs at `29/06/2026 17:37 -03` showed `cbabb12c`
dispatching 4-5 concurrent Opus requests and receiving upstream `429` twice:

```text
rate-pushback-retry bid=cbabb12c status=429 retry=1/2 pause=30.0 retry_after=0.0
aimd-shrink bid=cbabb12c status=429 max_concurrent=3 ...
rate-pushback-retry bid=cbabb12c status=429 retry=1/2 pause=30.0 retry_after=0.0
aimd-shrink bid=cbabb12c status=429 max_concurrent=1 ...
```

Second mitigation applied at `29/06/2026 17:40 -03`:

```bash
POST /ui/config key=max_concurrent value=3
POST /ui/config key=central_local_max_concurrent value=3
```

Post-second-mitigation health: local `cbabb12c` `lim_live=3`, `lim_hard=3`;
central `cbabb12c` `lim_live=2`, `lim_hard=3`; both status `allowed`, 5h
utilization `33%`, 7d utilization `26%`. The falsifier is a fresh central
`429` while central live concurrency is `<=3`.

### Durable fix

Keep `THROTTLE_MAX_HOLD_RETRY_AFTER_S` default at `60`, not `15`, so common
short 30s upstream throttles are absorbed. Multi-hour account-window
`Retry-After` values must still fast-fail or trigger the stale-credential 401
nudge.

Do not raise `CLAUDE_API_THROTTLE_MAX` above `3` for the active Claude Code
Opus-heavy fleet unless fresh central evidence shows clean operation above
that. The SOTA follow-up is not another static number: implement adaptive
send-rate / weighted admission keyed by model and request size, using
`Retry-After`, 429 density, queue delay, and client-disconnects as feedback.

### Follow-up: disconnected queued clients

At `29/06/2026 18:27:59 -03`, desktop restarted the local proxy into the
new package (`served` reset, pid changed from `573099` to `2633722`). Socket
activation kept the port mostly available, but several in-flight upload bodies
reset during `request.read()`, then the restarted tabs created an Opus-heavy
burst. Local health showed `max=3`, active bearer `cbabb12c` `status=allowed`,
and no fresh upstream retries; central health also had `upstream_retries=0`.
The visible API errors were therefore not new Anthropic 429s. They were client
disconnects while queued/streaming:

```text
client-disconnect ... bid=cbabb12c ... model=claude-opus-4-8 ... no_upstream_retry=true
ConnectionResetError: [Errno 104] Connection reset by peer
```

Durable fix in PR #68: treat body-read resets as 499 client disconnects, and
check the aiohttp client transport after fair-queue admission and again after
any Retry-After wait. If the Claude tab has already gone away, release the
slot and do not forward to central/upstream.

## 07/07/2026 — phantom-401 storm: bounded queue wait (PR #83)

Fleet-wide `API Error: 401 InvalidHTTPResponse fetching /v1/messages?beta=true`
was NOT a real 401. With only 2/5 bearers usable (A expired without a
refreshToken, C empty), fair-queue waits reached 60-80 s. Claude Code aborts a
silent request at ~60 s and closes its socket; the proxy's late write then hit
a closing transport:

```text
client-disconnect where=first path=/v1/messages ... via=central
elapsed_ms=75123 exc='ClientConnectionResetError: Cannot write to closing
transport' no_upstream_retry=true
```

The truncated HTTP surfaced client-side as Node fetch `InvalidHTTPResponse`,
which claude-code misclassifies as a 401/login failure (matches
routatic/proxy#62). No knob bounded queue time — `max_hold_retry_after_s`
gates only Retry-After holds.

Durable fix in PR #83 (merged `c8431f8`; central deployed same day; desktop
pin bumped in NixOS #1175):

- `THROTTLE_QUEUE_MAX_WAIT_S` (default 30 s, hot-tunable, 0=off) bounds the
  fair-queue wait. Exceeding it returns a clean
  `503 + Retry-After: 5 + x-anthropic-throttle-queue-timeout: 1` while the
  client transport is still alive, so the SDK retries transparently.
- The bound is end-to-end: each tier forwards the remaining budget via
  `x-anthropic-throttle-wait-budget-ms` and the next tier takes
  `min(own knob, inherited)`; every central attempt restamps the remaining
  budget so pushback-retry sleeps cannot re-grant central a full window.
  Client-supplied copies (any case) are stripped and re-stamped canonically.
- A local tier relays the stamped 503 verbatim — exempt from pushback-retry
  and AIMD shrink (central queue depth is admission backpressure, not the
  bearer's upstream pushback). The stamp is only trusted from marker-bearing
  proxy responses; a raw upstream 503 carrying it is stripped and still
  shrinks.

Codex adversarial review ran three rounds and every finding was real:
round 1 caught the local+central stacking BLOCKER and the spoofable-header
MAJOR; round 2 caught the stale-budget-on-retry and mixed-case-duplicate
BLOCKERs; round 3 approved. Single-pass review would have shipped a >60 s
silence hole.

Capacity itself is the other half of the incident: the fleet needs account A
re-logged (Pedro-gated OAuth) — the proxy fix makes saturation *clean*, not
free. Residual: `max_hold_retry_after_s=60` can still hold one Retry-After
near the client's patience window; lower it via `/ui/config` if disconnect
logs persist at high queue depth.

### Capacity resolved — 3-account roster, A + C re-logged (07/07 later, live-verified)

The "needs account A re-logged" thread is closed. Accounts are wired via
`THROTTLE_ACCOUNT_CRED_PATHS` with `least_loaded` routing and
`THROTTLE_ACTIVE_CRED_PATH=/home/notroot/.claude/.credentials.json`:

- **A** — `~/.claude`, Max. Was EXPIRED with no `refreshToken` (could not
  auto-refresh — the incident trigger). Re-logged via the browser-automation
  Claude-OAuth flow (Browser Automation Cookbook § 0.11). Now carries a
  `refreshToken` + future `expiresAt`.
- **B** — `~/.claude-b`, Max. **7-day quota exhausted** — bearer `116856a0`
  holds `Retry-After ≈ 212003 s (~59 h)`; `least_loaded` auto-excludes it
  (#78). Rejoins when its 7d window resets.
- **C** — `~/.claude-c` (`phsb5321@gmail.com`), Max. Was the empty
  "missing credentials" account. Re-logged (same OAuth flow). Now has a
  `refreshToken` + future `expiresAt`.
- rbw "Claude Key" (`sk-ant-api03-…`) has **no credits** — do NOT wire it as a
  bearer.

Live evidence (07/07, this session): `/__throttle/health` → `central_status=up`,
`queued=0`, `max_concurrent=3`, three distinct bearers — two `allowed_warning`
at `util_7d ≈ 0.76`, one exhausted at `~59 h`. The **#83 bound is armed**:
`overrides.json` carries 11 keys, none is queue-wait, so `QUEUE_MAX_WAIT_S` runs
the code default **30 s** (`config.py`), well inside claude's ~60 s abort window.
The running store path
`wfvlmb5qjldi4vfakqjzvxp1fzzhzrz6-anthropic-throttle-proxy-0.1.0` exposes the
`queue_max_wait_s` editable knob → confirms it is the #83 build. (`/__throttle/health`
has no top-level `queue_max_wait_s` field — do not read its absence as "disabled";
the effective value lives behind `/ui/config`.)

Cosmetic caveat: both A and B `.claude.json` render
`oauthAccount = pedrobalbino@pm.me` (the two Proton aliases share one mailbox);
the OAuth bearers are distinct hashes (one exhausted), so routing sees two real
accounts regardless of the shared email label.

## 12/07/2026 - warning surcharge second-stage hotfix

PR #96 was directionally right but too conservative live. It made warning
accounts spendable under pressure, but `_WARNING_BACKPRESSURE_SURCHARGE=250`
still outweighed two queued requests (`queued * 100`). Under a HOME+DeliCasa
burst, the router kept preferring C until C had multiple queued Opus turns:
C reached `served=107` while A/B were `20/16`, then one local
`queue-wait-timeout` appeared. There were still no upstream `429`, no
credit-balance errors, and API-key routing stayed `off`.

Second-stage fix: lower `_WARNING_BACKPRESSURE_SURCHARGE` to `80`, below one
queued-request weight. That keeps warning accounts protected under light
inflight-only load, but spends them as soon as the non-warning account starts
queueing.

Live deployment:

- venv: `~/.local/state/anthropic-throttle-proxy/live-094-warning-backpressure-20260712-202014`
- service PID: `1539700`
- API key: still disabled/off
- 90s sample:
  `queue_wait_timeout=0 status429=0 rate_pushback=0 aimd_shrink=0 api_key_mentions=0 credit_balance=0 routes_A=0 routes_B=5 routes_C=15`

Validation in clean worktree `097-warning-surcharge`:

- `uv run ruff format --check src tests`
- `uv run ruff check src tests`
- `uv run pytest tests/test_proxy_helpers.py -q` -> `81 passed`
- `uv run pytest -q` -> `408 passed`, 2 existing aiohttp warnings

Durable PR: `yolo-labz/anthropic-throttle-proxy#97`.

## 12/07/2026 - reset-aware budget pacing + restart-stable cooldowns

After PR #97, live traffic still proved one gap: the router reacted to
warning/queue pressure, but it could still spend an account that was close to
its 5h reset cliff. At 22:19, bearer `765d8fa5` hit a real upstream 429 with
`Retry-After` to the 22:50 reset while 5h utilization was `0.99`. A live
service restart then exposed a second gap: Retry-After state was process-local,
so a restarted proxy forgot that cooling window and reprobed it once.

PR #98 changes the budget-paced allocator from utilization-only tie-breaking to
reset-aware admission:

- a request projected past `THROTTLE_UTILIZATION_TARGET` is not selectable;
- below the target, accounts burning ahead of their reset-window slope get an
  overpace debt surcharge;
- when unified headers show the binding window has reached the target, future
  dispatch on that bearer is paused until reset instead of waiting for a 429;
- optional `THROTTLE_RETRY_AFTER_STATE_FILE` persists bearer Retry-After floors
  across service restarts.

Live hotfix deployment:

- venv: `~/.local/state/anthropic-throttle-proxy/live-098-reset-aware-pacing-20260712-224355`
- service PID: `2422027`
- state file:
  `~/.local/state/anthropic-throttle-proxy/retry-after-state.json`
- API key: still disabled/off; no credits bought; no `/usage-credits`
- restore proof: log line
  `bearer-retry-after-restore bid=765d8fa5 remaining=299s`

Validation:

- `uv run ruff format --check src tests`
- `uv run ruff check src tests`
- focused routing/unified/Retry-After tests -> `16 passed`
- full suite -> `411 passed`, 2 existing aiohttp `NotAppKeyWarning` warnings

Desktop account inventory at deploy time: exactly three active credential
stores were present and integrated via `THROTTLE_ACCOUNT_CRED_PATHS`:
`A=~/.claude/.credentials.json`, `B=~/.claude-b/.credentials.json`,
`C=~/.claude-c/.credentials.json`. No additional local `.credentials.json`
stores were found on desktop.

## 18/07/2026 — OAuth telemetry isolation and Nix/Dokku activation

The fleet-wide temporary-limiting incident had a proven feedback-contamination
root cause. At 20:19:47, old desktop PID `1051165` received a `429` for bearer
`666a53af` on `GET /api/oauth/usage`; at 20:19:48 it scheduled the message
advisor for that bearer. An OAuth telemetry response had therefore poisoned
message admission even though no `/v1/messages` request caused the event.

Proxy PR #121 (squash `7b48c34568fcb0f4efad81eebaf703f957429029`)
isolates `/api/oauth/{usage,profile}` from message rate-limit, AIMD, retry-state,
and advisor feedback. NixOS PR #1294 (squash
`eeda7549a594eeb61b2724ffa25644fe2f329e87`) pins that exact proxy revision.
Deployment evidence on 18/07/2026:

- Desktop generation 1899 activated the package at
  `/nix/store/y8rda0047lwl8i3cfmrjf9w4a1gzklwi-anthropic-throttle-proxy-0.1.0`.
  PID `2284564` started at 20:28:00 with local hard/max 6; the final controlled
  diagnostic restart uses PID `2363844` from the same store, with the socket
  and five-second keepalive timer active.
- The first desktop activation was not a zero-inflight cutover: old PID
  `1051165` entered SIGTERM with inflight 6 at 20:26:51 and reached zero at
  20:28:00. The replacement logged five read-body client resets. All nine
  active panes recovered, while Herdr PID `2015` and the service on port 8767
  (PID `477735`) were unchanged. Future desktop activations must capture
  `inflight=0 queued=0` before crossing the service boundary.
- Server and macOS pulled Nix commit `eeda7549` and activated successfully.
  Their local proxies run the pinned package and report central `up` with
  upstream egress healthy.
- The server activation installed the declarative Dokku sync. It changed
  `CLAUDE_API_THROTTLE_MAX` from 8 to 6 and redeployed central container
  `7b1b52d2c0d3`. Central health reports hard max 6, fair queueing, healthy
  upstream egress, and bearer A recovered from initial AIMD cap 3 to live cap
  6. The drift-sync timer remains enabled as the persistence backstop.

The controlled state diagnostic separated stale contamination from real
message throttling:

- Bearer B (`47f0b262`) was preserved because its 20:23:49 event was a real
  `POST /v1/messages` with `Retry-After=117371`, originally valid until
  20/07/2026 05:00. Restart restoration bounded that persisted hold to the
  existing 900-second safety cap, so it expired locally at 18/07/2026 20:43.
  The boundary reprobe caused a bounded, user-visible burst: five queued B
  requests returned central queue-wait `503` at 20:43:00-01; B then served
  some `200` responses before four real message `429` responses at 20:43:08
  and 20:43:17 with `Retry-After` about 116212 seconds. Those POST responses,
  not telemetry, correctly re-persisted B to epoch `1784534400.56`
  (20/07/2026 05:00). At 20:43:39 B had zero in-flight/queued requests and the
  restored deadline remained live. The 900-second restore cap therefore does
  not preserve a longer proven upstream cooldown across restart and can expose
  this bounded reprobe error burst.
- The pre-fix C (`666a53af`) residue was removed while the proxy, socket, and
  keepalive timer were stopped. After restart, a fresh telemetry
  `GET /api/oauth/usage` returned `429`, but C stayed at retry 0 with
  `last_ratelimit=null`; there was no advisor call or AIMD shrink. This is the
  live end-to-end proof that PR #121 prevents telemetry poisoning.
- C then served three real message POSTs with status 200 before real
  `/v1/messages` requests returned `429` at 20:34:37 and 20:34:45 with
  `Retry-After≈6323` and reset 22:20. The proxy correctly re-persisted that
  message-path pause. B and C are therefore genuinely unavailable; A-only
  capacity now determines queue behaviour.
- From 20:34:46 onward, the post-diagnostic sample contained 44 message 200s,
  10 unrelated compressed-body 400s, two client-disconnect 499s, and no
  message 429 or 503. A direct default-model/ultracode probe returned exactly
  `HEALTHY_A_OK` through healthy A.

Client/UI verification: every visible Claude pane was normalized to the
default Opus 4.8 1M route plus ultracode effort. Pearson verifier `w23:p2`
made about 45 inference calls through `http://127.0.0.1:8765`, completed its
interrupted create/check/merge workflow, and merged notes-work PR #93 at
20:37:23 with GitGuardian green and no recurring temporary-limiting error.
The pre-B-boundary local sample contained 63 message 200s, 15 unrelated
compressed-body 400s, two client-disconnect 499s, and no new message 429 or
503 after C's legitimate pause was recorded.

At the 20:43 B boundary, `w23:p2` displayed the expected temporary-limiting
banner after a separate `gh` PR-create attempt had failed because it ran from
a detached checkout. At 20:44 the pane resumed exactly once, created main-vault
PR #635 with explicit repository/head arguments, and continued through healthy
A without another admission error. Herdr's final scan showed `w23:p2` working;
`w22:p2` was also working after its earlier recovery. The banner still visible
in `w22:p3` was 2h44 old with no post-error work and was not part of this
boundary event.

## 18/07/2026 — single-flight boundary revalidation, live closure

The 20:43 B event proved the remaining mechanism: after a restored or long
Retry-After deadline expired, multiple waiting requests could select the same
bearer before any one request revalidated it. Proxy PR #123 (squash
`e16d15900f73e9f82c1714738368f5db3f21cfd0`) adds a per-bearer half-open gate.
Exactly one message request owns the probe; sibling requests use a healthy
bearer or wait. A successful response opens the gate as soon as its 2xx headers
arrive, before SSE EOF. A 429 restores the deadline. OAuth telemetry never
participates in this message gate. The full suite passed `488` tests, Ruff,
mypy, CodeQL, the repository quality gates, and an independent cross-family
review.

NixOS PR #1296 (squash `02440207c32dee4a148b2a0f95a861d1548173a3`)
pins that revision with SRI
`sha256-kE5QPvSKYCWRcZFISqxeo9JMEoaM3TNrgZL86u26e5M=`. Its 57 checks plus
server closure passed. Desktop runs store path
`/nix/store/j89ax3x3km3zh3x48hk2g2fg6rrwxz62-anthropic-throttle-proxy-0.1.0`
as PID `3154589`; the live and boot closures both contain that package. The old
PID's final 56.9-second POST completed with status 200 at 22:05:42 before the
new PID started, so the service handoff dropped no request.

Central was deployed only after the 22:14:19 preflight simultaneously recorded
`inflight=0`, `queued=0`, and zero other Dokku deploy processes. Dokku advanced
from `7b48c34` to `e16d159` and runs CID `258003f5d6f`. Its first authenticated
A request produced exactly one `retry-probe-begin`, opened at 22:15:14 before
EOF, and completed with status 200. The live health payload exposes real
boolean `retry_probe_required`, `retry_probe_inflight`, and
`retry_probe_blocks_routing` fields; #121 returned no such fields.

The exact previously failing C boundary was then observed end to end:

- local: one C probe began at 22:20:03, opened at 22:20:09, and returned 200 at
  22:20:20;
- central: one C probe began at 22:20:04, opened at 22:20:09, and returned 200
  at 22:20:20;
- no second C request started before either gate opened; sampled queues stayed
  at zero, then ordinary C traffic continued with status 200;
- B later received one 200 probe, after which central budget pacing correctly
  re-armed its 7-day pause at 90% utilization until 20/07/2026 05:00.

The closing aggregate was local `120x 200, 0x 429, 0x 503` and central
`22x 200, 0x 429, 0x 503`. The single local 499 was an intentional manual
interruption of an unrelated Claude turn to obtain the pre-deploy drain; it was
not caused by the service handoff or upstream. This makes the incident fix
verified, including the literal reset boundary, rather than inferred from unit
tests or a quiet-period sample.

Reversal remains one code revert plus one Nix pin revert, each through its
normal protected-branch PR:

```bash
git revert e16d15900f73e9f82c1714738368f5db3f21cfd0
git revert 02440207c32dee4a148b2a0f95a861d1548173a3
```

For an immediate central-only runtime rollback, Dokku retains the previous
release: `dokku releases:rollback anthropic-throttle 1`.

## 19/07/2026 — independent post-cutover proof, recorded durably

The central cutover owner ran an independent proof pass after closure and
delivered it to the release aggregator as an ephemeral `mktemp` brief that was
deleted after sending. The aggregator session then exhausted its provider
quota, so the brief is recorded here from its authoring transcript instead.

Source-level causal proof of the single-flight gate: `try_begin_retry_probe`
returns `False` when the probe inflight flag is already set, so exactly one
task wins the lease, and the claim is synchronous and race-free. The next
request on an elapsed window claims the probe, dispatches, and
`finish_retry_probe(success)` opens the gate for all waiters; with no traffic
the gate stays armed harmlessly. `finish` also runs as a task done-callback,
so a crashed or cancelled probe still releases the inflight flag and a dead
holder cannot freeze the gate. Waiters are bounded by
`asyncio.wait_for(wait_deadline)` and fail as a clean 503, never infinitely.

Runtime falsification (wedge-versus-armed) on the paused C bearer: a wedge
would show queued requests while `retry_probe_inflight` stays false — requests
starving with no probe firing. Observed instead: zero queued, zero inflight,
gate armed. Nothing starves; this is a healthy lazy-armed half-open, not a
deadlock.

Next-morning re-verification (09:47): CID `258003f5d6f` still running with an
unbroken `served` counter (~9.7k requests overnight, no restart), 114 of 114
central bearers expose boolean `retry_probe_*` fields, C remains
`required=true blocks_routing=true inflight=false queued_total=0` with
`retry_after_until` equal to its 7-day reset, and A is open. Both tiers show
zero message 429/503 since cutover.

Operator note: the `retry_probe_*` fields live under `.bearers[<bid>].limiter`
(PR #573 zips the limiter snapshot into the bearer view). Querying them at the
bearer top level returns `null` and falsely mimics the pre-#123 signature —
check the nested path before diagnosing a runtime regression.

The cross-family adversarial gate on #123/#124 was discharged later the same
morning by GLM-5.2 (z.ai) through opencode — family-diverse per the routing
policy, source-grounded — replacing the originally owed Codex pass (its quota
resets 25/07/2026 20:22). Verdict: PASS on four dimensions: starvation-free
(the `finally` release plus the task done-callback safety net), no inflight
leak (the probe claim is synchronous and the done-callback is registered with
zero awaits in between, so cancellation cannot land inside a window that
leaks the lease), half-open dispatch blocked while a probe is inflight, and
no lost wakeup (a fresh `asyncio.Event` is swapped in on probe begin and set
on finish, so waiters never park on a stale already-set event). All four line
claims were re-verified against `main` at `326558a` before this record was
written. The owed-Codex caveat is withdrawn; #123 is fully closed.
