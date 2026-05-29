"""Prometheus metric definitions for the throttle proxy.

A single process-local ``CollectorRegistry`` keeps ``/metrics`` fully isolated
from the default global registry, so a uvicorn-style multi-worker setup could
not double-register. We currently run single-worker but keep the isolation.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "M_START_TIME",
    "M_REQUESTS",
    "M_TOKENS",
    "M_COST",
    "M_DURATION",
    "M_INFLIGHT",
    "M_QUEUED",
    "M_INFLIGHT_BEARER",
    "M_QUEUED_BEARER",
    "M_CLIENT_DISCONNECTS",
    "M_UPSTREAM_RETRIES",
    "M_CENTRAL_STATUS",
    "M_AIMD_MAX",
    "M_AIMD_SHRINKS",
    "M_AIMD_GROWS",
    "M_AIMD_OVERLOAD",
    "M_BODY_SHRINK_TRIMMED",
    "M_BODY_SHRINK_BYTES_SAVED",
    "M_RATELIMIT_REQUESTS_REMAINING",
    "M_RATELIMIT_TOKENS_REMAINING",
    "M_UTIL_5H",
    "M_UTIL_7D",
    "generate_latest",
]

REGISTRY = CollectorRegistry()
# Process start time, set once in proxy.main(). A step change in this gauge is
# a restart — the proxy can be SIGKILLed mid-stream (29/05/2026: 8 in-flight
# streams dropped, the only evidence was a single journald line). Surfacing it
# as a metric makes restarts visible as a jump in Grafana/Gatus.
M_START_TIME = Gauge(
    "anthropic_proxy_start_time_seconds",
    "Unix start time of the proxy process; a step change = a restart.",
    registry=REGISTRY,
)
M_REQUESTS = Counter(
    "anthropic_requests_total",
    "Requests processed by the throttle proxy, labeled by HTTP method, status, and model.",
    ["method", "status", "model"],
    registry=REGISTRY,
)
M_TOKENS = Counter(
    "anthropic_tokens_total",
    "Tokens parsed from Anthropic SSE usage blocks.",
    # kind label is one of: input | output | cache_read | cache_creation
    ["model", "kind"],
    registry=REGISTRY,
)
M_COST = Counter(
    "anthropic_cost_usd_total",
    "Estimated USD cost (from claude.com rate table) per token kind.",
    ["model", "kind"],
    registry=REGISTRY,
)
M_DURATION = Histogram(
    "anthropic_request_duration_seconds",
    "Wall-clock duration of forwarded requests (excluding queue wait).",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 25.0, 60.0, 120.0, 300.0, 600.0),
    registry=REGISTRY,
)
M_INFLIGHT = Gauge("anthropic_inflight", "Current in-flight requests.", registry=REGISTRY)
M_QUEUED = Gauge(
    "anthropic_queued",
    "Current queued requests waiting for the proxy limiter.",
    registry=REGISTRY,
)
# PR #562: per-bearer parallelism — distinct OAuth bearers run in distinct
# Semaphore slots so two Claude Max accounts on different hosts don't fight
# for one global slot. bearer_id = 8-hex-char sha256 of Authorization header.
M_INFLIGHT_BEARER = Gauge(
    "anthropic_inflight_per_bearer",
    "Current in-flight requests per bearer (8-hex sha256 of Authorization).",
    ["bearer"],
    registry=REGISTRY,
)
M_QUEUED_BEARER = Gauge(
    "anthropic_queued_per_bearer",
    "Current queued requests per bearer.",
    ["bearer"],
    registry=REGISTRY,
)
M_CLIENT_DISCONNECTS = Counter(
    "anthropic_client_disconnects_total",
    "Client disconnections during response stream (claude gave up / network blip).",
    registry=REGISTRY,
)
M_UPSTREAM_RETRIES = Counter(
    "anthropic_upstream_retries_total",
    "Upstream-error retries (via=central failover OR direct-upstream resilience).",
    registry=REGISTRY,
)
M_CENTRAL_STATUS = Gauge(
    "anthropic_central_status",
    "Central-tier health: 1=up, 0=down, -1=unknown (no central configured).",
    registry=REGISTRY,
)
# PR #575: AIMD ceiling per bearer + shrink counter.
M_AIMD_MAX = Gauge(
    "anthropic_aimd_max_concurrent",
    "Current AIMD ceiling (mutable per-bearer max_concurrent).",
    ["bearer"],
    registry=REGISTRY,
)
M_AIMD_SHRINKS = Counter(
    "anthropic_aimd_shrinks_total",
    "AIMD multiplicative-decrease events triggered by upstream rate pushback (429/503).",
    ["bearer", "status"],
    registry=REGISTRY,
)
M_AIMD_GROWS = Counter(
    "anthropic_aimd_grows_total",
    "AIMD additive-increase events after sustained successes past cooldown.",
    ["bearer"],
    registry=REGISTRY,
)
M_AIMD_OVERLOAD = Counter(
    "anthropic_overload_total",
    "Upstream 529 overloaded events (Anthropic-side capacity, not your usage). "
    "Does NOT shrink the ceiling; retry-after is still honored.",
    ["bearer"],
    registry=REGISTRY,
)
# PR #15: body_shrink counters. ``still_oversize`` label is "true" when the
# trim could not get the body under the soft cap (a single huge attachment
# in the last KEEP_TURNS messages — operator should chase client-side fix).
M_BODY_SHRINK_TRIMMED = Counter(
    "anthropic_body_shrink_trimmed_total",
    "POST /v1/messages bodies trimmed by the proxy to fit under Anthropic's 32MB cap.",
    ["model", "still_oversize"],
    registry=REGISTRY,
)
M_BODY_SHRINK_BYTES_SAVED = Counter(
    "anthropic_body_shrink_bytes_saved_total",
    "Bytes removed from POST /v1/messages bodies by tool_result trimming.",
    ["model"],
    registry=REGISTRY,
)
# Last-seen upstream rate-limit headroom per bearer (proactive-pacing signal).
M_RATELIMIT_REQUESTS_REMAINING = Gauge(
    "anthropic_ratelimit_requests_remaining",
    "Last-seen anthropic-ratelimit-requests-remaining header value.",
    ["bearer"],
    registry=REGISTRY,
)
M_RATELIMIT_TOKENS_REMAINING = Gauge(
    "anthropic_ratelimit_tokens_remaining",
    "Last-seen anthropic-ratelimit-tokens-remaining header value.",
    ["bearer"],
    registry=REGISTRY,
)
# OAuth (Claude Code Max/Pro) unified-window utilization (0..1). The OAuth
# regime is gated by a 5-hour rolling + 7-day weekly window rather than
# RPM/ITPM/OTPM, reported via anthropic-ratelimit-unified-*-utilization.
M_UTIL_5H = Gauge(
    "anthropic_ratelimit_unified_5h_utilization",
    "OAuth 5-hour rolling window utilization (0..1).",
    ["bearer"],
    registry=REGISTRY,
)
M_UTIL_7D = Gauge(
    "anthropic_ratelimit_unified_7d_utilization",
    "OAuth 7-day weekly window utilization (0..1).",
    ["bearer"],
    registry=REGISTRY,
)
