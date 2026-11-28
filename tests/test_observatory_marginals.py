"""Synthetic fixtures for the pure DP08 preparation seam. Never admission."""

import json
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from anthropic_throttle_proxy.ui.observatory_evidence import (
    Generation,
    Scope,
    SelectionRelation,
    Window,
    pair_evidence,
    selection_relation,
)
from anthropic_throttle_proxy.ui.observatory_marginals import prepare_marginals

GENERATION = Generation("fixture-owner", "fixture-only", "a" * 64)
MEMBER = "b" * 64
POPULATION = "gateway_requests_utc:all"
SAT = datetime(2026, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
WINDOWS = (
    Window(SAT - MINUTE, SAT, 60, True),
    Window(SAT, SAT + MINUTE, 60, True),
)


def document():
    return {
        "schema": "dp08-utc-marginals-v1",
        "exposure": "aggregate-only",
        "population": POPULATION,
        "generation": GENERATION.id,
        "manifest_sha256": GENERATION.manifest_sha256,
        "export_member_sha256": MEMBER,
        "generated_at_epoch": 1767225720,
        "unplaced_observed_rows": 3,
        "marginals": [
            {
                "bucket_unit": "minute",
                "bucket_start_utc": "2025-12-31T23:59:00",
                "observed_rows": 3,
                "output_tokens_observed_rows": 2,
                "output_tokens_observed_sum": 2**127 - 1,
            },
            {
                "bucket_unit": "minute",
                "bucket_start_utc": "2026-01-01T00:00:00",
                "observed_rows": 1,
                "output_tokens_observed_rows": 1,
                "output_tokens_observed_sum": 0,
            },
        ],
    }


def encode(doc):
    return json.dumps(doc, separators=(",", ":")).encode()


def prepare(body, **overrides):
    params = {
        "payload_sha256": sha256(body).hexdigest() if body is not None else "d" * 64,
        "generation": GENERATION,
        "export_member_sha256": MEMBER,
        "windows": WINDOWS,
        "bucket_unit": "minute",
    }
    params.update(overrides)
    return prepare_marginals(body, **params)


def test_bound_payload_preserves_exact_subset_zero_and_unplaced_count():
    body = encode(document())
    result = prepare(body)
    assert result.status == "observed" and result.reason is None
    assert result.value is not None
    first, second = result.value.buckets
    assert result.value.unplaced_observed_rows == 3
    assert result.value.export_member_sha256 == MEMBER
    assert result.value.generated_at_epoch == 1767225720
    assert result.value.bucket_unit == "minute"
    assert (first.observed_rows, first.known_rows, first.unknown_rows) == (3, 2, 1)
    assert type(first.value) is int and first.value == 2**127 - 1
    assert second.status == "observed" and second.value == 0
    assert (first.evidence.window, second.evidence.window) == WINDOWS
    assert first.evidence.measure == "output_tokens" and first.evidence.unit == "tokens"
    assert result.evidence.source.loaded_sha256 == sha256(body).hexdigest()
    assert result.evidence.source.generation is GENERATION
    assert result.evidence.scope == Scope(POPULATION)
    assert result.evidence.window == Window(WINDOWS[0].start, WINDOWS[1].end, 60, True)
    assert result.evidence.coverage.state == "partial"
    assert "unplaced_observations" in result.evidence.coverage.reasons
    assert first.evidence.coverage.state == "partial"
    assert second.evidence.coverage.state == "unknown"
    assert not result.evidence.coverage.refs and not result.evidence.coverage.gaps
    assert not hasattr(second, "complete_total")


def test_no_known_token_values_exposes_no_number_for_that_measure():
    doc = document()
    doc["marginals"][0].update(output_tokens_observed_rows=0, output_tokens_observed_sum=None)
    first = prepare(encode(doc)).value.buckets[0]
    assert first.status == "unknown" and first.value is None
    assert first.reason.code == "no_known_output_tokens"
    assert first.known_rows == 0 and first.unknown_rows == first.observed_rows == 3
    assert first.evidence.coverage.state == "partial"


def test_offset_windows_normalize_to_explicit_utc():
    tz = timezone(timedelta(hours=-3))
    windows = tuple(
        Window(w.start.astimezone(tz), w.end.astimezone(tz), half_open=True) for w in WINDOWS
    )
    assert (
        tuple(b.evidence.window for b in prepare(encode(document()), windows=windows).value.buckets)
        == WINDOWS
    )


def test_window_and_grain_are_not_inferred_from_the_payload():
    result = prepare(encode(document()), bucket_unit="hour")
    assert result.status == "rejected" and result.reason.code == "window_mismatch"
    assert result.value is None
    result = prepare(encode(document()), bucket_unit="second")
    assert result.status == "rejected" and result.reason.code == "unsupported_grain"


@pytest.mark.parametrize(
    "windows",
    [
        WINDOWS[::-1],
        (WINDOWS[0], Window(SAT + MINUTE, SAT + 2 * MINUTE, 60, True)),
        (Window(SAT - MINUTE, SAT, 30, True), WINDOWS[1]),
        (Window(SAT - MINUTE, SAT), WINDOWS[1]),
        (
            Window(SAT - MINUTE + timedelta(seconds=1), SAT + timedelta(seconds=1), half_open=True),
            WINDOWS[1],
        ),
        (Window(SAT - 2 * MINUTE, SAT - MINUTE, 60, True), WINDOWS[0]),
    ],
)
def test_nonmatching_expected_edges_rejects_entire_pair(windows):
    result = prepare(encode(document()), windows=windows)
    assert result.status == "rejected" and result.reason.code == "window_mismatch"
    assert result.value is None and result.evidence.window is None


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"payload_sha256": "0" * 64}, "payload_pin_mismatch"),
        ({"payload_sha256": "not-a-pin"}, "invalid_pin"),
        ({"generation": Generation("fixture-owner", "other", "a" * 64)}, "lineage_mismatch"),
        ({"generation": Generation("fixture-owner", "fixture-only", "c" * 64)}, "lineage_mismatch"),
        ({"export_member_sha256": "e" * 64}, "lineage_mismatch"),
        ({"requested_scope": Scope("native_calls_utc:all")}, "scope_mismatch"),
        (
            {"requested_scope": Scope(POPULATION, (("account", "fixture"),), "fixture-registry")},
            "scope_mismatch",
        ),
        ({"requested_scope": Scope(POPULATION, (("provider", "fixture"),))}, "scope_mismatch"),
    ],
)
def test_rejects_cross_scope_and_wrong_pins_without_values(changes, code):
    result = prepare(encode(document()), **changes)
    assert result.status == "rejected" and result.reason.code == code and result.value is None
    assert result.evidence.source is None and result.evidence.scope is None
    assert result.evidence.coverage.state == "rejected"


@pytest.mark.parametrize(
    "field", ["payload_sha256", "generation", "export_member_sha256", "windows", "bucket_unit"]
)
def test_absent_trusted_expectations_are_unknown_not_observed(field):
    result = prepare(encode(document()), **{field: None})
    assert result.status == "unknown" and result.reason.code == "missing_expectation"
    assert result.value is None and result.evidence.source is None
    assert selection_relation(result.evidence, Scope(POPULATION)) == SelectionRelation.UNSUPPORTED


@pytest.mark.parametrize(("unit", "step"), [("hour", 3600), ("day", 86400)])
def test_other_frozen_grains_do_not_subdivide(unit, step):
    width = timedelta(seconds=step)
    windows = (Window(SAT - width, SAT, step, True), Window(SAT, SAT + width, step, True))
    doc = document()
    for row, window in zip(doc["marginals"], windows, strict=True):
        row.update(bucket_unit=unit, bucket_start_utc=window.start.isoformat()[:-6])
    result = prepare(encode(doc), windows=windows, bucket_unit=unit)
    assert result.status == "observed" and result.value.bucket_unit == unit
    assert tuple(bucket.evidence.window for bucket in result.value.buckets) == windows
    assert result.evidence.coverage.state != "complete"


def test_two_explicit_empty_buckets_do_not_become_zero_usage():
    doc = document()
    for row in doc["marginals"]:
        row.update(observed_rows=0, output_tokens_observed_rows=0, output_tokens_observed_sum=None)
    result = prepare(encode(doc))
    assert (result.status, result.reason.code, result.value) == ("rejected", "empty_slice", None)


def test_absent_payload_distinct_from_supplied_empty_bytes():
    assert prepare(None).status == "missing"
    assert prepare(b"").status == "rejected"


@pytest.mark.parametrize(
    "key,value,code",
    [
        ("population", "native_calls_utc:all", "scope_mismatch"),
        ("exposure", "raw", "scope_mismatch"),
        ("schema", "w07-aggregate-v1", "wire_shape"),
        ("provider", "sensitive-fixture", "wire_shape"),
        ("generated_at_epoch", True, "counter_mismatch"),
        ("unplaced_observed_rows", -1, "counter_mismatch"),
        ("marginals", [], "wire_shape"),
    ],
)
def test_wrong_top_level_wire_is_rejected_without_echo(key, value, code):
    result = prepare(encode({**document(), key: value}))
    assert result.status == "rejected" and result.reason.code == code
    assert result.value is None and "sensitive-fixture" not in repr(result)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"account": "sensitive-fixture"}, "wire_shape"),
        ({"bucket_start_utc": "2025-12-31T23:59:00Z"}, "window_mismatch"),
        ({"bucket_start_utc": "2026-01-01T00:00:00"}, "window_mismatch"),
        ({"bucket_unit": "hour"}, "window_mismatch"),
        ({"observed_rows": True}, "counter_mismatch"),
        ({"observed_rows": 1}, "counter_mismatch"),
        ({"output_tokens_observed_rows": 0, "output_tokens_observed_sum": 0}, "counter_mismatch"),
        ({"output_tokens_observed_sum": None}, "counter_mismatch"),
        ({"output_tokens_observed_sum": 2**127}, "counter_mismatch"),
        ({"output_tokens_observed_sum": 1.0}, "invalid_json"),
    ],
)
def test_incompatible_bucket_does_not_leak_partial_pair(changes, code):
    doc = document()
    doc["marginals"][0].update(changes)
    result = prepare(encode(doc))
    assert result.status == "rejected" and result.reason.code == code
    assert result.value is None and "sensitive-fixture" not in repr(result)


def test_exact_byte_bound_duplicate_keys_and_non_json():
    body = encode(document())
    cap = body.ljust(65536, b" ")
    assert prepare(cap).status == "observed"
    assert prepare(cap + b" ").reason.code == "payload_size"
    duplicate = b'{"generation":"fixture-only",' + body[1:]
    assert prepare(duplicate).reason.code == "invalid_json"
    assert prepare(b"PAR1syntheticPAR1").reason.code == "invalid_json"
    assert prepare(bytearray(body)).reason.code == "payload_type"


def test_n14_keeps_population_global_and_different_source_bytes_unpairable():
    body = encode(document())
    first = prepare(body).value.buckets[0].evidence
    changed = prepare(body + b" ").value.buckets[0].evidence
    assert selection_relation(first, Scope(POPULATION)) == SelectionRelation.MATCHED
    filtered = Scope(POPULATION, (("provider", "fixture"),))
    assert selection_relation(first, filtered) == SelectionRelation.INDEPENDENT_GLOBAL
    assert pair_evidence(first, first).compatible
    assert pair_evidence(first, changed).mismatches == ("loaded_bytes_mismatch",)
