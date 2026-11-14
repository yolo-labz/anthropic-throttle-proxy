# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Multi-stage uv build per Astral's official Docker pattern.
# Optimised for Dokku: Dockerfile builder, EXPOSE 8765, /__throttle/health checked
# by Dokku's app.json.

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.2@sha256:6dbd7c42a9088083fa79e41431a579196a189bcee3ae68ba904ac2bf77765867 /uv /usr/local/bin/uv

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


FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c

# Build provenance — injected by CI via --build-arg, surfaced in logs.
ARG APP_VERSION=0.1.0
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

# curl is required by the Dokku app.json healthcheck (curl -fsS /__throttle/health).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Run as an unprivileged user: the container holds upstream credentials in its
# environment, so a process escape must not land as root. UID/GID are fixed so a
# bind-mounted state directory keeps stable ownership across restarts.
RUN groupadd --gid 10001 throttler \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/throttler throttler \
    && mkdir -p /home/throttler/.local/state \
    && chown -R throttler:throttler /home/throttler

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/throttler \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    APP_VERSION=${APP_VERSION} \
    GIT_SHA=${GIT_SHA} \
    BUILD_DATE=${BUILD_DATE} \
    THROTTLE_HOST=0.0.0.0 \
    THROTTLE_PORT=8765

# The app writes state under $XDG_STATE_HOME (falling back to ~/.local/state),
# both now owned by the unprivileged user. /app itself stays root-owned and
# read-only: the service never needs write access to its own code.
USER throttler

EXPOSE 8765

CMD ["throttler-gateway"]
