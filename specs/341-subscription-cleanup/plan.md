# Subscription dashboard cleanup

## Goal
Show the operator's declared current inventory without treating retired/offboarded sources as active subscriptions. Missing or exhausted meters do not prove cancellation. No provider probing, admission, credential or routing changes.

## Plan
Add optional defaults.hidden_subscriptions to the existing YAML display projection. Match exact row id or lane_id, retaining raw observations and metrics. Preserve defaults for old configs. Supply an anonymized current-board example: enrolled Codex A/C, Z.AI, Copilot, MiMo individual and assigned Team seats; no obsolete tier/model captions. Hide retired desktop, offboarded B/DeepSeek and unassigned placeholders; show_primary=false removes the retired primary's local CRIT display.

## Executable acceptance
- A loaded config hides only exact declared source ids and leaves source data unchanged.
- With the retired desktop hidden, active MiMo rows including unknown Team B remain visible.
- Unknown, stale and exhausted readings remain visible unless explicitly hidden.
- Absent hidden_subscriptions changes nothing; malformed types preserve the last good config with an error.
- The real collect/render path excludes hidden rows from capacity totals and does not probe the network.
- Current-board example has no fixed subscription/model tier caption; live observations remain authoritative.

## Delivery
Targeted pytest, full pytest, ruff, different-family advisory review, required CI, safe squash merge. Apply the accepted YAML through the existing live mtime loader. Any source rollout must preserve the dedicated UI process and not restart inference lanes or the host.
