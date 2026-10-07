# Retained receipt-delivery hook failures — 07/10/2026

These are reported command/tool observations, not a fabricated raw runner log.

- At19:26 BRT, normal source pre-commit refused the unformatted evidence-only
  verifier: code-slop score50, ten E501 errors, five warnings. No commit/push ran.
  Formatter plus explicit long-string wrapping corrected the ten errors; no
  threshold, exclusion or hook change.
- A direct `ruff check` of the formatted evidence script still reports S603 at
  its subprocess helper. The argv are constructed only by this local read-only
  receipt script, no shell or CLI/untrusted report argument. An executable
  allowlist now checks git/systemctl/nix-store. This finding is retained, not
  suppressed and not claimed as a Ruff-clean evidence script. Normal slop gate
  treats this as its existing warning class, not a reduced threshold.
- The proxy's production source Ruff/full pytest acceptance remains the exact
  protected #323 CI receipt; copying an evidence script is not new runtime code.
- Earlier small admission refused memory24751092KiB below24GiB, before any
  commit payload executed. No bypass/fallback or scratch-only evidence.
- A later verifier recheck rejected integral86% because its ad-hoc receipt
  asserted `86.0%`, while the existing consumer deliberately formats `:g`.
  Read-only actual row inspection confirmed report86.0 / UI86%, same value.
  Corrected the evidence predicate to the actual source's formatter; no runtime
  change, relaxed numeric equivalence, cached-green substitution or deleted data.
