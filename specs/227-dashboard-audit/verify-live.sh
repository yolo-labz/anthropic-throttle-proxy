#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
exec python3 specs/227-dashboard-audit/verify-live.py
