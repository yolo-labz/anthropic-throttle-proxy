from datetime import UTC, datetime

import pytest

from anthropic_throttle_proxy import api_capacity, api_credits
from anthropic_throttle_proxy.ui.observatory_constraints import (
    ConstraintKind,
    ConstraintSource,
    ConstraintState,
    project_api_constraints,
)

NOW = datetime(2026, 11, 28, 16, 30, tzinfo=UTC).timestamp()


def _snapshot(bearers, accounts=()):
    return api_capacity.snapshot(
        credits=("ok", {"schema": 1, "accounts": list(accounts)}),
        bearer_state=bearers,
        candidate_bid=None,
        api_label="test",
        now=NOW,
    )


def _bearer(**headers):
    return {
        "last_ratelimit_at": NOW - 30,
        "last_ratelimit": {
            f"anthropic-ratelimit-{key.replace('_', '-')}": value for key, value in headers.items()
        },
    }


def test_independent_account_and_dimension_evidence_from_pr127_snapshot():
    snap = _snapshot(
        {
            "api-key": _bearer(
                requests_remaining="0",
                requests_limit="10",
                requests_reset="2026-11-28T16:31:00Z",
                tokens_remaining="2",
                tokens_limit="90",
                tokens_reset="2026-11-28T16:29:59Z",
                input_tokens_remaining="9",
                input_tokens_reset="2026-11-28T16:33:00Z",
                output_tokens_remaining="6",
            ),
            "raw-secret-bearer": _bearer(
                requests_remaining="4",
                requests_limit="20",
                requests_reset="2026-11-28T16:35:00Z",
            ),
        }
    )
    rows = project_api_constraints(snap, now=NOW)
    by_key = {(r.scope, r.dimension): r for r in rows if r.source == ConstraintSource.API_HEADERS}
    exhausted = by_key["Proxy snapshot: api:caller", "requests"]
    assert (exhausted.kind, exhausted.state, exhausted.remaining, exhausted.denominator) == (
        ConstraintKind.CONTINUOUS_REFILL,
        ConstraintState.CURRENT,
        0,
        10,
    )
    assert exhausted.used is None
    assert exhausted.provider is None  # compatible header family, not provider identity
    assert exhausted.observed_at == "2026-11-28T16:29:30Z"
    assert exhausted.reset_at == "2026-11-28T16:31:00Z"
    expired = by_key["Proxy snapshot: api:caller", "tokens"]
    assert expired.state == ConstraintState.EXPIRED
    assert expired.reset_at == "2026-11-28T16:29:59Z"
    assert expired.remaining is expired.denominator is None
    input_meter = by_key["Proxy snapshot: api:caller", "input_tokens"]
    assert input_meter.unit == "input tokens"
    assert input_meter.remaining == 9 and input_meter.denominator is None
    assert input_meter.reset_at == "2026-11-28T16:33:00Z"
    assert by_key["Proxy snapshot: api:caller", "output_tokens"].state == ConstraintState.UNKNOWN
    other = by_key["Proxy snapshot: api:observed-1", "requests"]
    assert (other.remaining, other.denominator, other.reset_at) == (4, 20, "2026-11-28T16:35:00Z")
    assert "raw-secret-bearer" not in repr(rows)


@pytest.mark.parametrize(("offset", "state"), [(0, "current"), (60, "expired"), (271, "stale")])
def test_cache_replay_cannot_refresh_an_observation(offset, state):
    snap = _snapshot(
        {
            "api-key": _bearer(
                requests_remaining="8",
                requests_limit="10",
                requests_reset="2026-11-28T16:31:00Z",
                tokens_remaining="7",
                tokens_limit="10",
                tokens_reset="2026-11-28T17:31:00Z",
            )
        }
    )
    # Later render time does not change the sampled clock. Long-reset tokens isolate the age gate.
    dimension = "tokens" if state == "stale" else "requests"
    row = next(
        r for r in project_api_constraints(snap, now=NOW + offset) if r.dimension == dimension
    )
    assert row.state.value == state
    assert row.observed_at == "2026-11-28T16:29:30Z"
    assert row.remaining == (8 if state == "current" else None)


def test_legacy_values_and_unadmitted_sources_do_not_become_constraints():
    empty = api_capacity.snapshot(
        credits=("absent", {"schema": 1, "accounts": []}),
        bearer_state={},
        candidate_bid=None,
        api_label="test",
        now=NOW,
    )
    empty["header_lanes"] = [
        {
            "lane": "api:caller",
            "rate_limit": {
                "state": "observed",
                "values": {"requests_limit": 100, "requests_remaining": 99},
            },
        }
    ]
    empty["usage_tokens"] = 500
    empty["subscription"] = {"denominator": 100, "remaining": 75}
    rows = project_api_constraints(empty, now=NOW)
    assert {row.kind for row in rows} == set(ConstraintKind)
    assert all(row.state == ConstraintState.UNAVAILABLE for row in rows)
    assert all(
        row.remaining is row.used is row.denominator is row.observed_at is None for row in rows
    )
    assert (
        "subscription"
        in next(r for r in rows if r.kind == ConstraintKind.SUBSCRIPTION_WINDOW).reason
    )


def test_spend_limit_is_a_separate_receipt_local_amount_not_balance():
    raw = {
        "schema": 1,
        "accounts": [
            {
                "id": "candidate",
                "provider": "anthropic",
                "accountId": "private-account",
                "workspaceId": "private-workspace",
                "authorized": True,
                "currency": "USD",
                "observedAt": "2026-11-28T16:29:30Z",
                "validUntil": "2026-11-28T16:40:00Z",
                "credit": {"status": "available", "remaining": 100.00},
                "spendLimit": {"remaining": 12.25, "enforced": False},
                "source": "receipt",
            }
        ],
    }
    snap = _snapshot({}, api_credits.normalize(raw, NOW)["accounts"])
    rows = project_api_constraints(snap, now=NOW)
    row = next(r for r in rows if r.kind == ConstraintKind.SPENDING_LIMIT)
    assert row.source == ConstraintSource.API_SPEND_RECEIPT
    assert row.state == ConstraintState.CURRENT
    assert row.provider == "anthropic"
    assert row.remaining == 12.25 and row.unit == "USD" and row.enforced is False
    assert row.used is row.denominator is row.reset_at is None
    assert "private-account" not in repr(rows) and "private-workspace" not in repr(rows)
    expired = next(r for r in project_api_constraints(snap, now=NOW + 600) if r.kind == row.kind)
    assert expired.state == ConstraintState.STALE and expired.remaining is expired.enforced is None


def test_duplicate_snapshot_lane_does_not_double_attribute():
    snap = _snapshot(
        {
            "api-key": _bearer(
                requests_remaining="3",
                requests_limit="9",
                requests_reset="2026-11-28T16:31:00Z",
            )
        }
    )
    snap["header_lanes"].append(snap["header_lanes"][0])
    rows = project_api_constraints(snap, now=NOW)
    rate = next(r for r in rows if r.kind == ConstraintKind.CONTINUOUS_REFILL)
    assert rate.state == ConstraintState.UNAVAILABLE and rate.scope is rate.remaining is None


@pytest.mark.parametrize("bad_clock", ["2026-11-28T16:30:01Z", "2026-11-28T16:29:30", None])
def test_invalid_observation_never_borrows_render_clock_or_source_claim(bad_clock):
    snap = _snapshot(
        {
            "api-key": _bearer(
                requests_remaining="3",
                requests_limit="9",
                requests_reset="2026-11-28T16:31:00Z",
            )
        }
    )
    snap["header_lanes"][0]["rate_limit"]["observed_at"] = bad_clock
    row = next(r for r in project_api_constraints(snap, now=NOW) if r.dimension == "requests")
    assert row.state == ConstraintState.UNKNOWN and row.remaining is row.denominator is None
    assert row.reset_at == "2026-11-28T16:31:00Z"
