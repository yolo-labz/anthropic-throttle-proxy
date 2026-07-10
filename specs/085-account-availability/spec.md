# Feature Specification: Continuous Account Availability

**Feature Branch**: `085-account-availability`  
**Created**: 2026-07-09  
**Status**: Draft  
**Input**: User description: "we need to solve everything" — the account-availability problem set surfaced across this session: every configured Claude account must always be authenticated and bearing load, with **no downtime**, browser re-login **automated** (by code or a small model) so it is never a manual event, and accounts **correctly segregated** so they never revoke one another.

## Context (why this exists)

The operator runs a fleet of Claude Code / agent sessions through a self-hosted throttle proxy that spreads traffic across several Claude subscription accounts. Two failures motivated this feature:

1. **The 09/07 outage**: two credential stores were pointed at the **same** account. Because a subscription login rotates its refresh token on every refresh, two stores refreshing one account mutually revoked each other — both died. Only the one distinct third account carried the entire fleet.
2. **Manual recovery**: restoring a dead account required a human-driven browser login. The operator's requirement is explicit — logins must be automated (code or a small model) so re-authentication is **never** a manual interruption, and the fleet suffers **no downtime** ("no time off").

Research confirmed the hard constraint: there is **no way to keep a subscription session alive forever** — an absolute login-lifetime cap exists. Therefore "always logged in" is achieved not by eliminating logins but by making recovery **proactive, automated, and invisible**: refresh ahead of expiry, re-authenticate before the cap forces an outage, and keep a healthy account absorbing load at all times.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - All accounts always carry the load (Priority: P1)

Every account the operator has configured is authenticated and actively serving a fair share of the fleet's traffic. No account sits dead or idle while another burns toward its budget cap. This is the MVP: it is the exact request that opened this work ("make sure all accounts are bearing the usage load").

**Why this priority**: This is the core value. If all configured accounts are live and load-balanced, the fleet has its full combined budget and no single account collapses under concentration. Everything else (automation, zero-downtime, segregation) exists to *sustain* this state.

**Independent Test**: Configure N distinct accounts; drive fleet traffic; verify that after a short warm-up every account is eligible for routing and each has served requests, and that load spreads toward the least-utilized account rather than concentrating on one.

**Acceptance Scenarios**:

1. **Given** N distinct authenticated accounts, **When** the fleet generates sustained traffic, **Then** every account is a routing target and each serves a non-zero share, with new traffic biased toward the least-utilized account.
2. **Given** one account has been dead, **When** it is restored, **Then** it re-enters rotation automatically and begins bearing load without operator action.
3. **Given** one account approaches its weekly budget cap, **When** traffic continues, **Then** new traffic shifts to accounts with remaining budget while the hot account is drained, and no account is pushed past its cap while another sits materially idle.

---

### User Story 2 - Zero-human re-login (Priority: P2)

When an account's session can no longer be kept alive by refresh (revoked, expired beyond refresh, or hit its absolute cap), the system re-authenticates it **fully automatically** — including any email verification / magic-link step and the consent click — with **zero keystrokes** from the operator. Automation tolerates login-page changes.

**Why this priority**: Dead sessions are inevitable (rotation edge cases, absolute cap). If recovery needs a human, the operator's "never an issue / no time off" requirement is unmet. Automating recovery converts every login into a background event.

**Independent Test**: Invalidate one account's session; observe the system detect the dead state, run the full browser login unattended (retrieving the verification link from the mailbox itself), and return the account to service — with no human interaction and no other account interrupted.

**Acceptance Scenarios**:

1. **Given** an account whose refresh no longer works, **When** the system detects the dead session, **Then** it completes a full unattended re-authentication and the account returns to routable state.
2. **Given** the login page layout has changed, **When** automated login runs, **Then** it still completes (a scripted happy path with an automated fallback that adapts), or it fails **loudly** with captured evidence rather than silently.
3. **Given** re-authentication needs a verification code/link, **When** login runs, **Then** the system retrieves it from the account's mailbox and completes login without exposing any secret in logs or automation prompts.

---

### User Story 3 - No downtime (make-before-break) (Priority: P3)

No client request ever fails because of authentication. Sessions are refreshed ahead of expiry; an account showing the first sign of trouble is **drained** (stops receiving new requests, finishes in-flight) before it can return an error; at least one healthy account is always ready to absorb load; and the mandatory pre-cap re-login is scheduled proactively while other accounts carry the fleet.

**Why this priority**: "No time off" is a first-class requirement. Reactive-after-failure recovery still drops requests. Zero-downtime requires *make-before-break*: establish/validate the replacement before retiring the old.

**Independent Test**: Force an account through a full expiry-and-cap cycle under live traffic; verify zero client-visible auth failures throughout, that the account was drained before any error, and that a standby carried its load during recovery.

**Acceptance Scenarios**:

1. **Given** an account nearing session expiry, **When** the refresh window arrives, **Then** the session is renewed before expiry with no request failing.
2. **Given** an account begins returning trouble signals, **When** the signal is observed, **Then** new traffic stops routing to it and in-flight requests complete, before any client sees a failure.
3. **Given** an account will hit its absolute login cap, **When** the cap approaches, **Then** its automated re-login is performed proactively (in available capacity from other accounts), so the cap never coincides with the account being needed.
4. **Given** one account is mid-recovery, **When** requests arrive, **Then** a healthy standby serves them and the fleet does not stall.

---

### User Story 4 - Correct account segregation (Priority: P4)

Each credential store maps to a **distinct** account and is isolated so accounts can never revoke one another. The system refuses (or loudly flags) any configuration where two stores resolve to the same account — the exact failure that caused the 09/07 outage — and guarantees only one refresher ever acts on a given account at a time.

**Why this priority**: This is the root-cause guard. Without it, the P1 state is fragile — a duplicate silently collapses two accounts into one and triggers mutual revocation. It ranks below P1–P3 only because it *protects* them rather than delivering the visible outcome directly.

**Independent Test**: Attempt to configure two stores with the same account; verify the system detects the duplicate and refuses/alerts. Separately, drive concurrent refreshes of one account and verify no revocation occurs (single-writer guarantee holds).

**Acceptance Scenarios**:

1. **Given** two credential stores resolving to the same account, **When** the system evaluates its configuration, **Then** it flags the duplicate and does not treat them as two independent budgets.
2. **Given** multiple processes could refresh one account's session, **When** a refresh is due, **Then** exactly one refresh occurs and the account is never invalidated by a concurrent refresh.
3. **Given** an account is refreshed, **When** the new session is issued, **Then** it is persisted atomically so no reader ever sees a half-written credential and no stale session is ever replayed.

---

### User Story 5 - Operator sees state, is alerted only when needed (Priority: P5)

The operator can see, at a glance, which accounts are logged in, which are bearing load, each account's time-to-expiry, and which need attention. The system alerts the operator **only** when automated recovery has failed or a genuinely human-only step is required — never on transient, self-healed events.

**Why this priority**: Observability makes the guarantee trustworthy and turns "is it working?" into a glance. It is P5 because the fleet functions without it, but the operator cannot rely on the guarantee without it.

**Independent Test**: View the dashboard/health surface and confirm per-account auth state, load share, and time-to-expiry are present; trigger a self-healing event and confirm no alert; trigger an unrecoverable event and confirm exactly one actionable alert.

**Acceptance Scenarios**:

1. **Given** the dashboard, **When** the operator looks, **Then** each account shows: logged-in state, whether it is bearing load, its time-to-expiry, and its budget utilization.
2. **Given** a transient failure that auto-heals, **When** it resolves, **Then** no operator alert is raised.
3. **Given** an unrecoverable failure, **When** automation exhausts its attempts, **Then** exactly one actionable alert is raised, naming the account and the required action.

---

### User Story 6 - Secure remote client access (Priority: P4)

The operator can point a **remote** client (e.g. a work laptop / WSL box reachable over a private VPN) at the same throttler so it shares the account pool — without exposing the operator's accounts to anyone else. Access is restricted to the operator's private network and authenticated; the proxy never becomes an open relay for the operator's tokens.

**Why this priority**: The pool's value grows when every one of the operator's machines can use it, not just the host. It ranks P4 (alongside segregation) because it must not be enabled before the access controls exist — an exposed proxy that attaches the operator's tokens to *any* caller is a credential-leak, and extra remote volume must not push the pool into rotate-to-evade territory.

**Independent Test**: From a remote machine on the operator's private network, set the client's base endpoint to the throttler and confirm requests are served from the pool; from a machine NOT on the private network (or without the shared secret), confirm access is refused.

**Acceptance Scenarios**:

1. **Given** a remote client on the operator's private network with the shared secret, **When** it points its base endpoint at the throttler, **Then** its requests are served from the account pool exactly as a local client's are.
2. **Given** a caller without network authorization or the shared secret, **When** it reaches the throttler's address, **Then** access is refused and no account token is ever attached on its behalf.
3. **Given** a remote client adds sustained load, **When** the pool approaches any account's cap, **Then** the system spills to the sanctioned overflow rather than rotating accounts to exceed a cap (FR-020), so remote volume never converts into a limit-evasion pattern.

### Edge Cases

- **Duplicate account across stores** (the 09/07 root cause): two stores resolve to one account → mutual refresh revocation. MUST be detected and prevented.
- **External revocation mid-flight**: the account is logged in elsewhere, rotating/orphaning the shared session. MUST be detected and recovered.
- **All accounts near cap simultaneously**: no warm standby available. MUST degrade gracefully (fair queue / clear signal) rather than fail silently.
- **Interactive captcha during automated login**: automation cannot solve it. MUST either clear it via the supported path or fail loudly with evidence, never hang silently.
- **Verification email delayed or suppressed** (e.g., datacenter-IP filtering): MUST poll/retry within a bounded window and surface a clear failure if it never arrives.
- **Auth-endpoint rate-limits the refresh** (rejection without a retry hint): MUST back off, not hammer (hammering extends the lockout).
- **Absolute login cap on the last healthy account** while others are also capped: MUST be anticipated (proactive re-login staggered so accounts never all cap together).
- **Recovery in progress for the exact account a request needs**: MUST route to a standby, not block.
- **Clock skew**: expiry math MUST carry margin so skew never causes a "fresh" account to be used past expiry.
- **Login-page change breaks scripted automation**: MUST fall back to adaptive automation or fail loudly.
- **Provider suspends an account for the account-sharing mechanism**: MUST be surfaced distinctly (this is a policy failure, not a token failure) so the operator can act.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST keep every configured account continuously authenticated (a valid session available for routing), except for the minimal window of an in-progress automated recovery.
- **FR-002**: System MUST distribute live traffic across ALL authenticated accounts so each bears a fair share; no authenticated account may sit idle while another is near its budget cap.
- **FR-003**: System MUST renew each account's session ahead of its expiry (proactively), and MUST NOT rely on a failed request to trigger renewal.
- **FR-004**: System MUST ensure exactly one refresher acts on any single account at a time, so a concurrent refresh can never invalidate that account's session.
- **FR-005**: System MUST guarantee each credential store maps to a DISTINCT account, and MUST detect and refuse or loudly flag any two stores that resolve to the same account.
- **FR-006**: When an account's session becomes unrecoverable by refresh (revoked / expired-beyond-refresh / absolute-cap), System MUST re-authenticate it automatically with no human interaction.
- **FR-007**: Automated re-authentication MUST complete the entire login — including any email magic-link/verification-code step and the consent step — without a human, and MUST tolerate reasonable login-page changes (adaptive fallback) or fail loudly with captured evidence.
- **FR-008**: System MUST perform an account's mandatory pre-cap re-authentication PROACTIVELY, using spare capacity from other accounts, so a hard cap never coincides with the account being needed.
- **FR-009**: System MUST stop routing NEW traffic to an account at the first credible sign of trouble (drain) and let in-flight requests complete, so a failing account does not return a client-visible error.
- **FR-010**: System MUST keep at least one healthy account able to absorb load at all times (warm standby), so any single account's recovery never stalls the fleet.
- **FR-011**: System MUST never expose raw credentials or tokens in logs, metrics, dashboards, or automation prompts (only non-reversible identifiers).
- **FR-012**: System MUST record, per account, its authentication state, time-to-expiry, budget utilization, and last-recovery outcome.
- **FR-013**: System MUST persist a newly obtained session atomically, so a reader never observes a partially-written credential and a stale session is never replayed.
- **FR-014**: System MUST back off on repeated authentication-endpoint rejections (rate limits) rather than retry aggressively.
- **FR-015**: System MUST alert the operator ONLY when automated recovery has exhausted its attempts or a genuinely human-only step is required — never on transient, self-healed events — and each alert MUST name the account and the required action.
- **FR-016**: The operator MUST be able to see, at a glance, which accounts are logged in, which are bearing load, and which need attention.
- **FR-017**: The account-sharing mechanism the system uses MUST operate within the provider's acceptable-use terms; where the chosen mechanism carries a suspension risk, that risk MUST be an explicit, documented, operator-acknowledged decision (see Open Decision in Assumptions).
- **FR-018**: System MUST distinguish a provider policy/suspension failure from an ordinary token failure and surface them differently, because they require different operator responses.
- **FR-019**: System MUST use pure subscription-OAuth passthrough — it MUST NOT inject an API key or a static auth token, and the upstream target MUST remain the provider's real API host — so subscription traffic is never silently redirected to per-token API-usage billing (the `ANTHROPIC_BASE_URL`-gateway trap).
- **FR-020**: System MUST spread load across accounts while each stays below its own caps, and MUST NOT rotate accounts to sustain total throughput past a single account's cap. When the pool genuinely approaches exhaustion, System MUST spill to a sanctioned overflow (pay-as-you-go API credits) rather than rotate-to-evade — because cross-account rotation to beat a cap is an enforced acceptable-use violation.
- **FR-021**: When the proxy is reachable beyond the host, access MUST be restricted to the operator's private network AND authenticated (shared secret / network ACL); the proxy MUST refuse unauthorized callers and MUST NOT attach any account token on their behalf. The proxy MUST NOT be exposed to the public internet.
- **FR-022**: System MUST preserve the genuine client's request surface on forwarded traffic (user-agent, the client's own request body and markers) and only substitute the operator's own account bearer — so per-request traffic remains indistinguishable from ordinary single-user use and is never in the harness-spoofing class.
- **FR-023**: System SHOULD keep each account's observable usage shaped like a single human operator's (avoid concentrating all accounts' peaks behind one origin in a way that reads as coordinated pooling), acknowledging that account-linkage enforcement can suspend all linked accounts at once.

### Key Entities

- **Account**: a distinct Claude subscription identity. Attributes: stable identifier (e.g. login email), authentication state (logged-in / refreshing / draining / dead / recovering), session expiry time, absolute login-lifetime remaining, budget utilization (short-window and weekly), last-recovery outcome.
- **Credential store**: an isolated container binding exactly one Account's live session. Invariant: 1:1 with a distinct Account.
- **Session**: the live authentication material for an Account. Has an expiry and a (single-use, rotating) renewal capability; must have exactly one writer.
- **Recovery event**: an automated re-authentication attempt for an Account. Outcome: success / failed / human-required, with captured evidence on failure.
- **Routing decision**: selection of which Account serves a given request, driven by each Account's health and remaining budget (prefers healthy, least-utilized; excludes draining/recovering accounts).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: At any sampled moment, 100% of configured accounts are authenticated and eligible to serve traffic (no account dead longer than the recovery window).
- **SC-002**: Zero client-visible request failures attributable to authentication or session expiry over a rolling 7-day window.
- **SC-003**: When an account's session dies, it is restored automatically within 10 minutes in ≥95% of cases, with zero human interaction.
- **SC-004**: Under sustained load, every authenticated account serves a non-zero share, and no account exceeds its weekly budget while another sits more than 25% below it.
- **SC-005**: Zero cross-account revocation events (no account is ever invalidated by another account's or process's activity).
- **SC-006**: The operator receives an alert only after automated recovery fails — measured as 0 alerts for events that self-heal, and exactly 1 actionable alert per genuinely unrecoverable event.
- **SC-007**: Every mandatory pre-cap re-authentication completes before the account's hard cap is reached (0 recoveries that coincide with the account being unavailable for load).
- **SC-008**: A duplicate-account misconfiguration is detected before it can cause a revocation (100% of duplicate configurations flagged prior to serving traffic).
- **SC-009**: Zero occurrences of subscription traffic being silently billed as per-token API usage (0 api-key/static-token injections on the subscription path).
- **SC-010**: Total pooled load never sustains throughput past a single account's cap by rotation; when demand exceeds combined subscription capacity, 100% of the excess is served by the sanctioned overflow path, not by cross-account rotation.
- **SC-011**: Zero unauthorized callers ever receive a token-attached response from the proxy (every remote request is network-authorized and carries the shared secret, or is refused).

## Assumptions

- The operator possesses ≥2 (currently 3) **distinct** valid Claude subscription accounts and controls the mailboxes used for their logins; automating these logins is authorized self-service on the operator's own machine.
- A verification-email channel for each account is reachable programmatically (for magic-link / code retrieval during automated login).
- No mechanism can keep a subscription session alive indefinitely — an absolute login-lifetime cap exists — so the guarantee is "always effectively available via fast proactive automated recovery," not "never logs in."
- The provider does not currently offer a browserless (device-code) re-authentication for this client; if/when it does, that path supersedes browser automation for recovery. A long-lived issued token, if available for the subscription tier, may reduce recovery frequency and should be preferred where it exists.
- The existing throttle proxy (routing, utilization-header pacing, per-account refresh) is the integration point; this feature extends it rather than replacing it.
- Small-model / adaptive browser automation is acceptable for the login fallback (the operator explicitly allowed "code or a smaller model"); the default is a deterministic scripted happy-path with an adaptive fallback only when the script breaks or a challenge appears.
- A sanctioned pay-as-you-go API key is available (or can be provisioned) to serve as the overflow path (FR-020); its metered cost is accepted as the price of staying within acceptable-use rather than rotating accounts to evade caps.
- All of the operator's machines that use the pool are reachable over one private network (VPN / mesh) and the proxy can be bound/served on it with an ACL; the proxy is never placed on the public internet.

### Decision Record — segregation architecture (resolved 2026-07-09, research + adversarial verification)

**Decision: keep per-request "guarded pooling" (proxy selects the operator's own account per request and swaps only the Authorization bearer), governed by three hard rules that reduce the residual ban-risk from MODERATE to low.** Rejected: migrating to per-profile (loses the per-request load-spread the operator explicitly wants) and pure-sanctioned-only (loses the combined subscription capacity).

Evidence basis (all via research + an adversarial refutation pass):
- Owning multiple personal Max accounts is **not** a ToS violation (Anthropic engineer, on record). Enforcement targets **reselling, credential-sharing with others, and third-party clients that spoof the Claude Code harness** — none of which this is.
- Detection is **content-based** (the CLI's system-prompt billing marker + a streaming classifier), **not** TLS/fingerprint/header-order. A passthrough that keeps the **genuine** Claude Code client intact and only swaps the operator's own OAuth bearer is **effectively indistinguishable per-request** from that account being used normally.
- The adversarial pass downgraded the risk from LOW to **MODERATE** on two real, enforced vectors that the three rules below directly mitigate: (1) **rotation-to-evade-limits** is a named violation; (2) **account-linkage** bans (shared IP/device/card) hit **all** accounts at once with a ~3% appeal-success rate; and (3) `ANTHROPIC_BASE_URL` + injected auth silently drops subscription traffic to **API-usage billing** (claude-code #20976).

The three governing rules are encoded as FR-019, FR-020, FR-021. All the general safety guards (distinctness, single-writer, drain, warm standby, automated recovery, observability) apply on top and are architecture-independent.

## Out of Scope

- Increasing any single account's budget or subscription tier (client-side/business action, not solvable here).
- Defeating the provider's anti-pooling detection or evading acceptable-use terms.
- Solving interactive captchas that require a human (these MUST fail loudly to the operator, per FR-007/FR-015).
- **Distributing credentials to other hosts.** Remote machines (US6) reach the pool by talking to the single proxy that holds the accounts; the account credentials themselves are never copied to the remote box. Multi-host credential replication is separate infrastructure and out of scope here.
- Running the pool for anyone other than the operator, or exposing it publicly (explicitly forbidden by FR-021).
