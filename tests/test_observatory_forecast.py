"""Synthetic quota contract checks; no production identities or policy values."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from anthropic_throttle_proxy.ui.observatory_evidence import (
    Coverage,
    Evidence,
    Generation,
    Scope,
    SourceRef,
    Window,
)
from anthropic_throttle_proxy.ui.observatory_forecast import (
    MAX_SAMPLES,
    ForecastPolicy,
    estimate_exhaustion,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
GENERATION = Generation("fixture", "g1", "1" * 64)
SOURCE = SourceRef("quota-history", "2" * 64, GENERATION)
CHECKPOINT = SourceRef("quota-checkpoint", "3" * 64, GENERATION)
SCOPE = Scope("quota", (("account", "a1"), ("provider", "fixture")), "accounts")
WINDOW = Window(START, START + timedelta(seconds=125), half_open=True)
EVIDENCE = Evidence(
    SOURCE, WINDOW, SCOPE, "allowance", "requests", Coverage("complete", refs=(CHECKPOINT,))
)
POLICY = ForecastPolicy(min_span_s=120, max_gap_s=90, max_horizon_s=600)


def at(seconds):
    return START + timedelta(seconds=seconds)


def sample(seconds, remaining, **changes):
    return {
        "evidence": EVIDENCE,
        "snapshot": {
            "constraint_kind": "subscription-window",
            "native_meter_key": ("quota-source", "requests", "window-1"),
            "account_scope": {"kind": "stable", "namespace": "accounts", "account_key": "a1"},
            "unit": "requests",
            "remaining": remaining,
            "used": None,
            "limit": None,
            "denominator": None,
            "observed_at": at(seconds),
            "observed_reset_at": at(900),
        }
        | changes,
    }


def admitted(*samples):
    return {"status": "observed", "value": samples, "evidence": EVIDENCE, "reason": None}


FRESHNESS = {
    "observed_at": at(120).timestamp(),
    "published_at": at(125).timestamp(),
    "now": at(130).timestamp(),
    "max_age_s": 30,
    "generation": "g1",
}


def fit(*samples, policy=POLICY, **freshness_changes):
    return estimate_exhaustion(
        admitted(*samples), freshness=FRESHNESS | freshness_changes, policy=policy
    )


def test_estimate_bounds_are_actual_observed_rate_sensitivity_not_confidence():
    # 10 used in 60s, then 20 in 60s. At 20 remaining: 120s / 30 * 20 = 80s;
    # the two observed rates give 60s and 120s. All offsets start at t=120.
    result = fit(sample(0, 50), sample(60, 40), sample(120, 20))
    assert result.state == "estimated"
    assert result.estimate.method == "elapsed_mean_with_adjacent_rate_envelope"
    assert result.estimate.eta_after_observation_s == 80
    assert result.estimate.uncertainty_after_observation_s == (60, 120)
    assert result.estimate.horizon_after_observation_s == 600
    assert result.estimate.fit_observations == (at(0), at(120))
    assert result.estimate.sample_count == 3
    assert result.estimate.freshness.source_age_s == 10
    assert result.observed_reset_at == at(900)


def test_uneven_sampling_uses_elapsed_weighting_not_mean_of_interval_rates():
    # Both intervals lose 10. Equal-weight rate averaging would give 90s,
    # whereas the elapsed 120s spent 20, leaving another 120s at that pace.
    result = fit(sample(0, 40), sample(30, 30), sample(120, 20))
    assert result.state == "estimated"
    assert result.estimate.eta_after_observation_s == 120
    assert result.estimate.uncertainty_after_observation_s == (60, 180)


def test_current_snapshot_envelope_is_not_a_history():
    data = admitted(*good_samples()) | {"value": sample(120, 20)["snapshot"]}
    result = estimate_exhaustion(data, freshness=FRESHNESS, policy=POLICY)
    assert_unknown(result, "history_continuity_unknown")


def test_no_history_even_with_fresh_clock_is_unknown():
    result = estimate_exhaustion(None, freshness=FRESHNESS, policy=POLICY)
    assert (result.state, result.reason) == ("unknown", "quota_history_not_supplied")
    assert result.estimate is None
    assert result.observed_reset_at is None


def assert_unknown(result, reason):
    assert (result.state, result.reason, result.estimate) == ("unknown", reason, None)


def good_samples():
    return sample(0, 50), sample(60, 40), sample(120, 20)


@pytest.mark.parametrize("status", ["missing", "unknown", "rejected"])
def test_unadmitted_restart_cannot_be_bridged_even_if_rows_were_supplied(status):
    rejected = admitted(*good_samples()) | {
        "status": status,
        "reason": {"code": "restart_continuity_unattested", "message": "no restart fence"},
    }
    assert_unknown(
        estimate_exhaustion(rejected, freshness=FRESHNESS, policy=POLICY),
        "restart_continuity_unattested",
    )


def test_observed_status_cannot_substitute_for_independent_coverage():
    gapped = replace(
        EVIDENCE,
        coverage=Coverage(
            "partial", ("restart_gap",), (CHECKPOINT,), (Window(at(61), at(75), half_open=True),)
        ),
    )
    assert_unknown(
        estimate_exhaustion(
            admitted(*good_samples()) | {"evidence": gapped}, freshness=FRESHNESS, policy=POLICY
        ),
        "history_continuity_unknown",
    )


@pytest.mark.parametrize(
    ("freshness_changes", "reason"),
    [
        ({"max_age_s": 10}, "stale_history"),
        ({"max_age_s": None}, "missing_age_policy"),
        ({"max_age_s": True}, "invalid_age_policy"),
        ({"published_at": None}, "missing_publication_time"),
        ({"published_at": at(119).timestamp()}, "observation_after_publication"),
        ({"now": at(119).timestamp()}, "source_time_in_future"),
        ({"now": True}, "invalid_reference_time"),
        ({"observed_at": at(121).timestamp()}, "freshness_history_mismatch"),
        ({"generation": "other"}, "freshness_history_mismatch"),
    ],
)
def test_freshness_is_bound_to_last_actual_observation(freshness_changes, reason):
    result = fit(*good_samples(), **freshness_changes)
    assert_unknown(result, reason)
    assert result.observed_reset_at == at(900)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"account_scope": {"kind": "stable", "namespace": "accounts", "account_key": "a2"}},
            "identity_kind_or_scope_mismatch",
        ),
        (
            {"native_meter_key": ("quota-source", "requests", "window-2")},
            "constraint_kind_or_meter_mismatch",
        ),
        (
            {"native_meter_key": ("quota-source", "other-meter", "window-1")},
            "constraint_kind_or_meter_mismatch",
        ),
        ({"constraint_kind": "concurrency"}, "constraint_kind_or_meter_mismatch"),
        ({"unit": "tokens"}, "incompatible_unit"),
        ({"observed_at": at(60)}, "non_monotone_observation_times"),
        ({"observed_at": at(20)}, "non_monotone_observation_times"),
        ({"observed_at": at(1200)}, "invalid_or_uncovered_observation_time"),
        ({"observed_at": at(120).replace(tzinfo=None)}, "invalid_or_uncovered_observation_time"),
        ({"observed_reset_at": at(901)}, "reset_boundary"),
        ({"observed_reset_at": None}, "reset_boundary"),
    ],
)
def test_a_mixed_or_discontinuous_segment_is_not_trimmed_or_sorted(changes, reason):
    start, middle, _ = good_samples()
    assert_unknown(fit(start, middle, sample(120, 20, **changes)), reason)


@pytest.mark.parametrize("kind", ["refill", "spend", "concurrency", "pacing"])
def test_unsupported_kinds_cannot_borrow_the_fixed_window_model(kind):
    assert_unknown(
        fit(*(sample(s, r, constraint_kind=kind) for s, r in [(0, 50), (60, 40), (120, 20)])),
        "unsupported_constraint_kind",
    )


def test_source_local_account_is_not_promoted():
    local = {"kind": "source_local", "producer_scope": "fixture", "native_key": "a1"}
    assert_unknown(
        fit(sample(0, 50, account_scope=local), *good_samples()[1:]), "account_scope_unbound"
    )


def test_global_checkpoint_does_not_attest_account_coverage():
    global_evidence = replace(EVIDENCE, scope=Scope("quota", identity_namespace="accounts"))
    samples = tuple(item | {"evidence": global_evidence} for item in good_samples())
    assert_unknown(
        estimate_exhaustion(
            admitted(*samples) | {"evidence": global_evidence}, freshness=FRESHNESS, policy=POLICY
        ),
        "account_scope_unbound",
    )


def test_mixed_source_or_generation_provenance_is_not_joined():
    other_generation = Generation("fixture", "g2", "4" * 64)
    foreign = replace(
        EVIDENCE,
        source=replace(SOURCE, generation=other_generation),
        coverage=Coverage("complete", refs=(replace(CHECKPOINT, generation=other_generation),)),
    )
    assert_unknown(
        fit(*good_samples()[:2], sample(120, 20) | {"evidence": foreign}),
        "sample_evidence_mismatch",
    )


def test_thin_and_widely_spaced_histories_return_distinct_causes():
    assert_unknown(fit(*good_samples()[1:]), "insufficient_observations")
    assert_unknown(fit(sample(15, 50), *good_samples()[1:]), "insufficient_observations")
    assert_unknown(fit(sample(0, 50), sample(100, 40), sample(120, 20)), "observation_gap")
    assert_unknown(fit(*([sample(0, 1)] * (MAX_SAMPLES + 1))), "history_exceeds_sample_bound")


def test_zero_remaining_is_exhausted_not_a_zero_second_eta():
    result = fit(*good_samples()[:2], sample(120, 0))
    assert (result.state, result.reason) == ("exhausted_at_observation", "observed_zero_remaining")
    assert result.estimate is None


def test_flat_and_unsustained_depletion_are_not_unlimited_or_guessed_etas():
    flat = fit(sample(0, 20), sample(60, 20), sample(120, 20))
    assert (flat.state, flat.reason, flat.estimate) == (
        "no_depletion",
        "flat_observed_series",
        None,
    )
    assert_unknown(
        fit(sample(0, 50), sample(60, 20), sample(120, 20)), "positive_rate_not_sustained"
    )
    assert_unknown(
        fit(sample(0, 50), sample(60, 19), sample(120, 20)), "remaining_increased_or_recalibrated"
    )


def test_entire_sensitivity_band_must_fit_horizon_and_precede_reset():
    assert_unknown(
        fit(*good_samples(), policy=replace(POLICY, max_horizon_s=119)),
        "projected_range_exceeds_horizon",
    )
    samples = tuple(
        sample(s, r, observed_reset_at=at(240)) for s, r in [(0, 50), (60, 40), (120, 20)]
    )
    result = fit(*samples)
    assert_unknown(result, "projected_range_reaches_reset")
    assert result.observed_reset_at == at(240)
    assert_unknown(
        fit(*samples, now=at(180).timestamp(), max_age_s=100), "projected_range_already_reached"
    )
    assert_unknown(fit(*samples, now=at(240).timestamp(), max_age_s=130), "reset_boundary")


def test_missing_reset_does_not_turn_into_the_forecast_horizon():
    samples = (sample(s, r, observed_reset_at=None) for s, r in [(0, 50), (60, 40), (120, 20)])
    assert_unknown(fit(*samples), "observed_reset_not_supplied")


def test_attested_limit_and_used_preserve_one_count_at_large_magnitude():
    limit = 10**30
    result = fit(
        *(sample(s, None, used=limit - r, limit=limit) for s, r in [(0, 3), (60, 2), (120, 1)])
    )
    assert result.state == "estimated"
    assert result.estimate.eta_after_observation_s == 60
    assert result.estimate.uncertainty_after_observation_s == (60, 60)


def test_changed_limit_cannot_look_like_burn():
    assert_unknown(
        fit(sample(0, 50, limit=50), sample(60, 40, limit=50), sample(120, 20, limit=30)),
        "limit_changed",
    )


def test_ratio_denominator_is_not_a_limit():
    samples = (sample(s, None, used=u, denominator=100) for s, u in [(0, 20), (60, 30), (120, 40)])
    assert_unknown(fit(*samples), "amount_or_unit_unavailable")


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf")])
def test_invalid_quantity_never_becomes_a_numeric_prediction(value):
    assert_unknown(fit(*good_samples()[:2], sample(120, value)), "invalid_quantity")


@pytest.mark.parametrize(
    "policy", [None, replace(POLICY, max_gap_s=0), replace(POLICY, min_span_s=True)]
)
def test_fit_limits_have_no_implicit_defaults(policy):
    assert_unknown(fit(*good_samples(), policy=policy), "missing_or_invalid_forecast_policy")
