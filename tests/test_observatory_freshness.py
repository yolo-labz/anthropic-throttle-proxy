"""Freshness must track source evidence, not publication or inspection activity."""

import pytest

from anthropic_throttle_proxy.ui.observatory_freshness import classify_freshness

NOW = 1_795_881_600.0


def classify(**changes):
    inputs = {
        "observed_at": NOW - 30,
        "published_at": NOW - 10,
        "now": NOW,
        "max_age_s": 60,
        "generation": "accepted-generation-7",
    }
    inputs.update(changes)
    return classify_freshness(**inputs)


@pytest.mark.parametrize(
    ("age", "state", "reason"),
    [
        (0, "fresh", "within_age_limit"),
        (59.999, "fresh", "within_age_limit"),
        (60, "stale", "age_limit_reached"),
        (60.001, "stale", "age_limit_reached"),
    ],
)
def test_source_age_limit_boundary(age, state, reason):
    result = classify(observed_at=NOW - age, published_at=NOW)
    assert (result.state, result.reason) == (state, reason)
    assert result.source_age_s == pytest.approx(age)
    assert result.publication_age_s == 0
    assert result.max_age_s == 60
    assert result.generation == "accepted-generation-7"
    assert result.evaluated_at == NOW


def test_republication_and_new_generation_do_not_refresh_old_observations():
    first = classify(observed_at=NOW - 600, published_at=NOW - 300)
    reexported = classify(
        observed_at=NOW - 600,
        published_at=NOW,
        generation="new-publication-of-old-observations",
    )
    assert first.state == reexported.state == "stale"
    assert first.source_age_s == reexported.source_age_s == 600
    assert (first.publication_age_s, reexported.publication_age_s) == (300, 0)
    assert reexported.generation == "new-publication-of-old-observations"


def test_repeated_inspection_crosses_limit_without_changing_source():
    first = classify()
    later = classify(now=NOW + 30)
    assert first.state == "fresh"
    assert later.state == "stale"
    assert (first.source_age_s, later.source_age_s) == (30, 60)
    assert first.observed_at == later.observed_at
    assert first.published_at == later.published_at
    assert first.generation == later.generation


def test_identical_source_can_have_different_explicit_view_policies():
    assert classify(max_age_s=20).state == "stale"
    assert classify(max_age_s=40).state == "fresh"


def test_epoch_zero_is_a_real_observation():
    result = classify(observed_at=0, published_at=0, now=0, max_age_s=1)
    assert result.state == "fresh"
    assert result.observed_at == result.published_at == result.evaluated_at == 0
    assert result.source_age_s == result.publication_age_s == 0


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("observed_at", "missing_observation_time"),
        ("published_at", "missing_publication_time"),
        ("max_age_s", "missing_age_policy"),
    ],
)
def test_missing_evidence_or_policy_is_unknown(field, reason):
    result = classify(**{field: None})
    assert (result.state, result.reason) == ("unknown", reason)
    if field != "observed_at":
        assert result.source_age_s == 30


@pytest.mark.parametrize(
    "value", [True, False, -1, float("nan"), float("inf"), float("-inf"), "30", 10**1000]
)
@pytest.mark.parametrize("field", ["observed_at", "published_at", "now", "max_age_s"])
def test_invalid_inputs_never_claim_freshness(field, value):
    result = classify(**{field: value})
    assert result.state == "unknown"
    expected = {
        "observed_at": "invalid_observation_time",
        "published_at": "invalid_publication_time",
        "now": "invalid_reference_time",
        "max_age_s": "invalid_age_policy",
    }
    assert result.reason == expected[field]


@pytest.mark.parametrize("value", [None, 0.0])
def test_absent_reference_or_nonpositive_policy_has_explicit_reason(value):
    if value is None:
        result = classify(now=None)
        assert result.reason == "invalid_reference_time"
        assert result.source_age_s is None
    else:
        result = classify(max_age_s=value)
        assert result.reason == "invalid_age_policy"
        assert result.source_age_s == 30
    assert result.state == "unknown"


@pytest.mark.parametrize(
    ("observed", "published", "reason"),
    [
        (NOW + 1, NOW + 1, "source_time_in_future"),
        (NOW - 30, NOW + 1, "source_time_in_future"),
        (NOW - 1, NOW - 2, "observation_after_publication"),
    ],
)
def test_inconsistent_clocks_are_unknown_without_implicit_skew(observed, published, reason):
    result = classify(observed_at=observed, published_at=published)
    assert (result.state, result.reason) == ("unknown", reason)
    assert result.source_age_s is None or result.source_age_s >= 0
    assert result.publication_age_s is None or result.publication_age_s >= 0
