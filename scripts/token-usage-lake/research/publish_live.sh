#!/usr/bin/env bash
# Refresh the published marts from the live lake, without ever writing to it.
#
# The lake stays single-writer: this script reads the lake's own completion receipt, copies the
# DuckDB file to the desktop (`sudo cp` on the Dokku host), proves the copy is consistent, builds
# the marts read-only, publishes an immutable generation to the object store and refreshes the
# local Parquet mirror that Rill reads. Nothing here schedules Dagster, restarts Dagster, or opens
# the lake for write.
#
# Usage: publish_live.sh [--dry-run]
# Exit 0 when the lake has not moved since the last publish, or when a new generation landed.
set -euo pipefail

HOST=${HOST:-ProxMox.Dokku}
LAKE=${LAKE:-/var/lib/dokku/data/storage/dagster/lake.duckdb}
RECEIPT=${RECEIPT:-/var/lib/dokku/data/storage/dagster/runtime/latest-complete.json}
STATE=${STATE:-$HOME/.local/state/token-usage-lake}
BASE=${BASE:-dokku-minio/token-lake/marts}
RILL=${RILL:-$STATE/rill/token-diet}
RESEARCH=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PY=${PY:-$RESEARCH/../../../.venv/bin/python}
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p "$STATE/warehouse-modeler" "$STATE/marts"
STATE_FILE="$STATE/publish-state.json"

say() { printf '%s %s\n' "$(date '+%d/%m/%Y %H:%M:%S')" "$*"; }

# 1. What has the lake finished? The receipt is written by Dagster after a completed materialization.
receipt_json=$(ssh -o BatchMode=yes "$HOST" "sudo -n cat '$RECEIPT'")
lake_run=$(printf '%s' "$receipt_json" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
generation="lake-${lake_run:0:8}"
last_run=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("lake_run", ""))' "$STATE_FILE" 2>/dev/null || true)

say "lake run $lake_run (generation $generation), last published ${last_run:-none}"
if [ "$last_run" = "$lake_run" ] && [ "$DRY_RUN" = 0 ]; then
  say "no change since the last publish; nothing to do"
  exit 0
fi

# 2. Copy the live file on the host (cheap, atomic-ish) and verify the transfer byte for byte.
copy="$STATE/warehouse-modeler/lake-$generation.duckdb"
if [ ! -f "$copy" ]; then
  remote=$(ssh -o BatchMode=yes "$HOST" \
    "sudo -n cp '$LAKE' /tmp/lake-copy.duckdb && sudo -n chown \$(id -u):\$(id -g) /tmp/lake-copy.duckdb && sha256sum /tmp/lake-copy.duckdb" \
    | awk '{print $1}')
  rsync -a "$HOST:/tmp/lake-copy.duckdb" "$copy"
  local_sum=$(sha256sum "$copy" | awk '{print $1}')
  if [ "$remote" != "$local_sum" ]; then
    say "REFUSING: transfer hash mismatch ($remote != $local_sum)"
    rm -f "$copy"
    exit 1
  fi
  ssh -o BatchMode=yes "$HOST" "rm -f /tmp/lake-copy.duckdb"
  say "copied $copy ($(stat -c %s "$copy") bytes, sha256 $local_sum)"
fi

# 3. Prove the copy is usable before it can influence a dashboard.
"$PY" - "$copy" "$receipt_json" <<'PY'
import json
import sys

import duckdb

path, receipt_json = sys.argv[1], sys.argv[2]
receipt = json.loads(receipt_json)
connection = duckdb.connect(path, read_only=True)
counts = {}
for table in ("calls", "requests", "tasks", "tool_invocations"):
    counts[table] = connection.execute(f"select count(*) from {table}").fetchone()[0]
    if counts[table] <= 0:
        raise SystemExit(f"REFUSING: {table} is empty in the copy")
expected = receipt["fact_counts"]
for table, expected_rows in expected.items():
    if table in counts and counts[table] < expected_rows * 0.9:
        raise SystemExit(f"REFUSING: {table}={counts[table]} far below the receipt's {expected_rows}")
print("copy verified:", json.dumps(counts, sort_keys=True))
PY

if [ "$DRY_RUN" = 1 ]; then
  say "dry run: stopping before build/publish"
  exit 0
fi

# 4. Build the marts from the verified copy, recording the lake's own receipt as provenance.
marts_dir="$STATE/marts/$generation"
rm -rf "$marts_dir"
provenance=$(mktemp)
printf '%s' "$receipt_json" | "$PY" -c '
import json, sys
receipt = json.load(sys.stdin)
json.dump(
    {
        "kind": "live_lake_copy",
        "lake_run_id": receipt["run_id"],
        "lake_accepted": receipt["accepted"],
        "lake_quality_status": receipt["data_quality"]["quality_status"],
        "lake_data_quality": receipt["data_quality"],
        "lake_fact_counts": receipt["fact_counts"],
        "lake_observed_through_epoch": receipt["observed_through_epoch"],
        "lake_completed_epoch": receipt["completed_epoch"],
    },
    sys.stdout,
)' > "$provenance"
(cd "$RESEARCH" && "$PY" -m marts --warehouse "$copy" --out "$marts_dir" --provenance "$provenance")
rm -f "$provenance"

# 5. Publish one immutable generation and re-hash what came back.
receipt=$(cd "$RESEARCH" && "$PY" -m objectstore --base "$BASE" publish "$marts_dir" --run-id "$generation" --deep)
printf '%s\n' "$receipt" | tail -n +1 >/dev/null
say "published $generation"

# 6. Refresh the Parquet mirror Rill reads, then restart it so the models re-reconcile.
mirror="$STATE/mirror/$generation"
rm -rf "$mirror"
(cd "$RESEARCH" && "$PY" -m objectstore --base "$BASE" download "$mirror" --run-id "$generation" >/dev/null)
mkdir -p "$RILL/data/marts"
rsync -a --delete "$mirror/" "$RILL/data/marts/"
if docker ps --filter name=rill-token-diet --format '{{.Names}}' | grep -q rill-token-diet; then
  docker restart rill-token-diet >/dev/null
  say "restarted rill-token-diet"
fi

"$PY" - "$STATE_FILE" "$lake_run" "$generation" <<'PY'
import json
import sys
import time

state_path, lake_run, generation = sys.argv[1], sys.argv[2], sys.argv[3]
state = {}
try:
    state = json.load(open(state_path))
except (OSError, ValueError):
    pass
state.update({"lake_run": lake_run, "generation": generation, "published_epoch": time.time()})
json.dump(state, open(state_path, "w"), indent=2)
PY
say "done: $generation published and mirrored"
