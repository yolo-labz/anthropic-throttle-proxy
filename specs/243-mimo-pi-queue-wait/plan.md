# 243 — MiMo Pi admission wait

## Scope / acceptance

New bounded source slice authorized 23/09/2026; not a relaunch of spec 227's frozen worker. Base: main `7ddf462`. Reuse `clients/pi-queue-wait` and main's retry-safety fixes. Add only `mimo-desktop` MiMo models at HTTP loopback port 8773, exact `/v1/chat/completions`; existing ZAI behavior must remain unchanged. No provider catalog, credentials, endpoint override, installation, service restart, Nix pin, or coordinator-pane changes.

An exact local queue rejection must stay pending for Retry-After + positive jitter, bounded by the existing wait/rejection ceilings. All other errors, uncertain usage, cancellation and partial output remain native-owned. Caller model/context/options must be preserved.

## Evidence before changes

23/09/2026 16:45–16:47 BRT, desktop, read-only:
- MiMo service active/running, PID 3006705, no drop-ins; persisted ExecStart and health build both `/nix/store/izqz6w190az1zjbzdm8q7s6wxlm570zi-anthropic-throttle-proxy-0.1.0`.
- Port 8773 health: fair queue; central URL empty (no central tier to probe). Admission: 2 slots, 120 s queue bound, measured service 130.908 s; idle at capture, not evidence of incident recovery.
- Journal `/v1/chat/completions`: 16:25:11 rejection advised 210 s; 16:25:13 → 212 s; 16:25:17 → 216 s; 16:25:25 → 225 s; 16:25:42 → 241 s. This confirms the screenshot's impatient retry sequence independently.
- Existing source rejects MiMo at its provider/model gate and only recognizes ZAI's 8766/path tuple. Existing suite on actual Pi 0.85.1: 119 pass, 0 fail/skip.

## Plan

1. Add hermetic actual-Pi-0.85.1 replay tests before production edits. Use real local HTTP fixture and caller fetch mapping from logical production URLs to an ephemeral loopback server, never the live proxy, no inference/auth. No production DI allowlist in these tests.
2. Prove RED on MiMo waiting/budget/registration. Preserve the existing ZAI suite and its loader/runtime preservation checks.
3. Select a fixed provider-specific port/path tuple in the shared engine; keep its existing retry logic. Overlay `api` + `streamSimple` only for the existing `mimo-desktop` provider. Prove real ModelRuntime preserves models.json catalog, endpoint, auth and headers and does not add a catalog when MiMo is absent.
4. Prove GREEN: exact positive gates and negatives, native maxRetries>0 cannot evade accounting, usage/content/prior-event fail-closed, actual partial text/thinking/tool streams, cancellation during wait, bounded wait/count, caller serialization/options, ZAI non-regression. Run full Python suite and ruff too.
5. Source-only PR delivery; own vault save-state with PR/artifact pointers. No runtime recovery claim without separately authorized activation.

## Falsifier

If main already waits 210–241 s plus jitter for a fully proven MiMo rejection in the actual adapter, the missing-lane diagnosis is false; if unrelated/native retries or ZAI behavior change, the proposed extension is unacceptable.

## Constitution

I–V preserved: client-only diff; no proxy hot-path dependencies, secrets, semaphore/AIMD, health, or upstream routing changes. Automated review is advisory under current fleet policy; GPT-only per task, no unavailable ZAI dispatch. Executable acceptance and real branch gates remain mandatory.
