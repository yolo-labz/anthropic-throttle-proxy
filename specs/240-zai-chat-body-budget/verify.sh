#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
uv sync --frozen --group dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q tests/test_routing_chat_budget.py tests/test_forwarding_text_only.py
uv run pytest -q
