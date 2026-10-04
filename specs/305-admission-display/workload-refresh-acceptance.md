# Workload/header refresh — source acceptance

Base: `8acd1618` (paired calibration PR #306), isolated `307-workload-refresh`.

Hypothesis: the full-page header keeps its workload label outside `#stats`,
while reload metadata identifies only revision and local/non-local mode; changing
workload with local=false unchanged can combine an old header and new stats.

## Smallest repair

Reuse the existing HTMX `hx-vals` / `HX-Refresh` contract with the already-visible
workload name. Serialize the complete attribute with Jinja `tojson` so quotes,
ampersands and angle brackets cannot corrupt it. No new scripts, routes, client
storage, collector, provider calls or status vocabulary. Missing workload metadata
retains the bare/legacy GET behavior. A build-revision change gets supported older
tabs onto the new full-page contract; a dead HTMX runtime still cannot refresh.

## Executable surface check

`test_existing_page_reloads_when_only_workload_changes` drives real full-page and
fragment render handlers with existing fake collectors and active network tripwires.
It extracts the polling metadata from the actual rendered HTML with HTMLParser,
changes only workload (including removal), checks HX-Refresh, then reads the new
full page and confirms polling no longer asks to reload. No live session is touched.

Red before repair: the rendered page had only `local` and `rev`; workload metadata
was absent (one failed, thirteen deselected). After the repair: **61 passed**, one
existing warning, 0.98s (admission-display, UI truth, UI assets); Ruff check and
format-check passed. Normal hosted gates and public PR evidence remain required.

This fixes the reproduced source-level mismatch only. It does not establish that
Pedro's original tab hit it, deploy a UI change, prove browser polling, or certify
provider capacity. Those remain live acceptance work.
