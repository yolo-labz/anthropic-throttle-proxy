#!/usr/bin/env bash
set -Eeuo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"
uv run pytest tests/test_lanes.py tests/test_ui_status.py
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
preview="${TMPDIR:-/tmp}/zai-subscription-preview.html"
uv run python tests/render_preview.py "$preview" >/dev/null
for needle in 'Pi routes' 'Z.AI' 'billing current' '$80/mo' 'renews 27/09' '5d 05h' '1h 52m'; do
  grep -Fq "$needle" "$preview"
done
rm -f "$preview"
! git grep -nE 'customerId|agreementNo|orderNo' -- \
  src/anthropic_throttle_proxy/lanes.py \
  src/anthropic_throttle_proxy/ui
printf 'zai subscription UI: PASS\n'
