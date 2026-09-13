#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
node tests/dashboard-refresh-check.mjs
# This is source verification only. Delivery requires verify-live.sh.
