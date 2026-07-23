# Spec 093 — tasks

One slice per row, smallest correct diff, each lands with its acceptance test
green + the full suite + ruff. Throttle-path → the speckit-make cross-family
adversarial panel (codex + Opus) must ALLOW each slice before merge. Execute
on/after 28/07 (kimi-k3 GA + codex recovery).

- [ ] **S1 — config knobs + no-op-when-unset invariant.** Add
      `THROTTLE_SPILLOVER_UPSTREAM` (default `""`), `THROTTLE_SPILLOVER_MODEL_MAP`
      (default `""`), `THROTTLE_SPILLOVER_GLIDE_THRESHOLD` (default = the
      `allowed_warning` line). Acceptance: with the upstream empty, the full
      existing suite passes byte-identical (invariant 4). No behavior change.
- [ ] **S2 — glide-saturation predicate.** A helper `_all_accounts_glide_saturated()`:
      true iff every configured account's binding window is `allowed_warning`+
      (or binding util ≥ threshold). Reads the unified state already in
      `bearer_state`. Acceptance test: a fleet with all accounts
      `allowed_warning` → true; one account `allowed` → false; accounts at 0.80
      with default threshold → false (invariants 1, 2).
- [ ] **S3 — spillover forward + model-remap (only-on-spillover).** In the
      account-selection path: when S2 is true AND `THROTTLE_SPILLOVER_UPSTREAM`
      is set, forward to the spillover origin path-preservingly with the request
      body's model rewritten via `THROTTLE_SPILLOVER_MODEL_MAP`. The `:8767`
      proxy stamps the Moonshot Bearer itself. Acceptance tests: spillover
      forward body model == mapped id; an Anthropic-bound (non-saturated)
      request keeps its original `claude-*` id (invariant 3); the never-trip
      test — all accounts `allowed_warning` → every new request routes to
      spillover, zero reach Anthropic `rejected` (invariant 1).
- [ ] **S4 — spillover-lane-down fallback.** Reuse the `fleetHealthUrls`/
      central-style probe: if the spillover lane is unhealthy, fall back to the
      existing least-bad Anthropic queue (rule 3) — never fail the request.
      Acceptance test: spillover upstream down + all accounts saturated →
      request routes to the Anthropic queue (not a hard fail) (invariant 5).
- [ ] **S5 — observability + end-to-end acceptance.** Counter
      `anthropic_spillover_total{to=kimi}` + one log line per spillover
      decision (which account states triggered it). End-to-end: a simulated
      fleet that drives accounts into `allowed_warning` under sustained load
      reaches ≥0.95 binding utilization with zero `rejected` trips, the
      residual served by spillover (invariant 6 + the goal bar).
- [ ] **S6 — Nix wire-through.** Expose the three knobs in the
      `lib/throttle-proxy-instance.nix` factory + the Anthropic HM module, set
      them on the desktop `:8765` instance (`THROTTLE_SPILLOVER_UPSTREAM=
      http://127.0.0.1:8767`, the model map). Default null elsewhere = no-op.
      Acceptance: `:8765` config evaluates; the `:8765/ui` Fleet panel shows the
      spillover lane.

## Verification bar (the done-state, externalized)
- `uv run pytest` green (incl. the new invariant tests S1–S5); `ruff` clean.
- speckit-make cross-family panel (codex reward-hacking + Opus correctness +
  codex security) all ALLOW each slice.
- A live or simulated run showing ≥0.95 binding utilization + zero `rejected`
  on the desktop fleet before declaring the objective met (invariant 1 is the
  hard floor; "exactly 100%" is not the bar — see the spec's honest limit).
