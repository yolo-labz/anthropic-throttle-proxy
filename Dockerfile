# syntax=docker/dockerfile:1.7
# Multi-stage uv build per Astral's official Docker pattern.
# Optimised for Dokku: Dockerfile builder, EXPOSE 8765, /health checked
# by Dokku's app.json.

FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1) install deps without the project itself — cacheable layer
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project

# 2) copy source + install the project as a non-editable package
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim

# Build provenance — injected by CI via --build-arg, surfaced in logs.
ARG APP_VERSION=0.1.0
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

# curl is required by the Dokku app.json healthcheck (curl -fsS /health).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    APP_VERSION=${APP_VERSION} \
    GIT_SHA=${GIT_SHA} \
    BUILD_DATE=${BUILD_DATE} \
    THROTTLE_HOST=0.0.0.0 \
    THROTTLE_PORT=8765

EXPOSE 8765

# Use `python -m` so __main__ is the entry — avoids hidden console-scripts.
CMD ["python", "-m", "anthropic_throttle_proxy"]
