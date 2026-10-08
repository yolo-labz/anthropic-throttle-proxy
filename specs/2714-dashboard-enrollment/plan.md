# Dashboard enrollment and verdict scope

08/10/2026. Scope: catalogue, display projection, template/assets and focused
acceptance. Base: merged proxy #331, `9b2db9529b514620ba15a1cb84d67164884663a0`.

Hypothesis: configured placeholders are counted as active seats because the
dashboard never joins the native enrollment manifest, while the local verdict
is presented as a fleet verdict. A native manifest enrolling B or a mismatch
between the deployed UI files and this baseline would falsify this diagnosis.

Read-only evidence: deployed revision `baeac143242b`; all five relevant deployed
UI/catalogue files match the baseline; 11 rendered rows; native schema-1 registry
enrolls Codex A/C, not B. The YAML still declares B. The local CRIT verdict is
valid for refused Anthropic credentials; it is not a verdict on MiMo capacity.

Implementation:

1. Read only Codex meter membership from the existing generated native registry,
   bounded and validated. Missing/invalid registry means unknown enrollment.
2. Separate unenrolled and unmatched catalogue entries from active capacity
   counts/joins without deleting historical readings or configured captions.
3. Label local/workload verdict scope visibly and accessibly. Label the local
   report clock explicitly; preserve independent source freshness.
4. Include catalogue code in the existing asset revision hash.

Acceptance uses synthetic inputs and the real collection/projection/template
path. No vendor/model probes, live controls, settings, grants, billing, activation
or Nix changes. Use the admitted desktop small lane for checks; no server job
while root's Notes acceptance owns its reserve. Deliver through normal hooks,
PR/CI and confirmed merge. Root owns combined Nix pin and activation; a merged
source change is not a live activation claim. Rollback is one revert PR.
