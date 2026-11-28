"""Bound loaded bytes, not labels, and do not project unknowns as measurements."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from anthropic_throttle_proxy import usage_summary
from anthropic_throttle_proxy.ui import observatory
from anthropic_throttle_proxy.ui.observatory_evidence import (
    Coverage,
    Evidence,
    Generation,
    Scope,
    SelectionRelation,
    SourceRef,
    Window,
    pair_evidence,
    selection_relation,
    summary_evidence,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
WINDOW = Window(START, START + 2 * MINUTE, 60, half_open=True)
GENERATION = Generation("example_authority", "example_generation", "a" * 64)
POPULATION = Scope("gateway_requests_utc:all")
FACT = SourceRef("gateway_marginal", "b" * 64, GENERATION)
COVERAGE_PROOF = SourceRef("checkpoint_coverage", "c" * 64, GENERATION)
UNKNOWN = Coverage("unknown", ("continuity_unverified",))
OBSERVED = Evidence(FACT, WINDOW, POPULATION, "requests", "recorded_calls", UNKNOWN)


def _summary_doc(output):
    row = {
        "provider": "example",
        "model": "model-a",
        "pane": None,
        "role": "turn",
        "requests": 1,
        "fresh_input": 4,
        "cache_write": 0,
        "cache_read": 0,
        "output": output,
        "reasoning": None,
    }
    return {
        "schema": usage_summary.SCHEMA,
        "generated_at": WINDOW.end.isoformat(),
        "coverage": {
            "window_start": WINDOW.start.isoformat(),
            "window_end": WINDOW.end.isoformat(),
            "files_scanned": 2,
            "files_skipped": 0,
        },
        "rows": [row],
    }


def _summarize(summary, **selection):
    """Exercise the protected projection, not a second measure/filter implementation."""
    view = observatory.build_view(summary, selection, now=summary.generated_at.timestamp())
    actual = view["selected"]
    filters = tuple((k, actual[k]) for k in ("provider", "model", "role") if actual[k])
    return view, summary_evidence(
        summary,
        measure=actual["measure"],
        filters=filters,
        unknown_values=view["total"]["unknown"],
    )


def test_replaced_file_does_not_rebind_already_loaded_input_to_new_bytes(tmp_path):
    path = tmp_path / "summary.json"
    first = json.dumps(_summary_doc(7)).encode()
    second = json.dumps(_summary_doc(9)).encode()
    path.write_bytes(first)
    loaded_first = usage_summary.load(path)
    path.write_bytes(second)
    old_view, old = _summarize(loaded_first, measure="output")
    new_view, new = _summarize(usage_summary.load(path), measure="output")
    assert old_view["total"]["value"] == 7 and new_view["total"]["value"] == 9
    assert (
        old.source.loaded_sha256 == old_view["loaded_sha256"] == hashlib.sha256(first).hexdigest()
    )
    assert (
        new.source.loaded_sha256 == new_view["loaded_sha256"] == hashlib.sha256(second).hexdigest()
    )
    assert old.source.generation is None and new.source.generation is None
    assert old.window == new.window and not old.window.half_open
    assert old.measure == "output" and old.unit == "tokens"
    pair = pair_evidence(old, new)
    assert not pair.compatible and pair.expected is old and pair.observed is new
    assert "generation_mismatch" in pair.mismatches
    assert pair_evidence(old, old).compatible


def test_rejected_rows_skipped_files_and_nulls_stay_partial_under_actual_filter(tmp_path):
    doc = _summary_doc(7)
    first = doc["rows"][0]
    doc["coverage"]["files_skipped"] = 1
    doc["rows"] += [first | {"output": None}, first | {"output": -1}]
    path = tmp_path / "summary.json"
    path.write_bytes(json.dumps(doc).encode())
    view, envelope = _summarize(usage_summary.load(path), provider="example", measure="output")
    assert view["headline"] == "≥ 7" and view["matched_rows"] == 2
    assert envelope.scope == Scope("summary_recorded_calls", (("provider", "example"),))
    assert envelope.coverage == Coverage(
        "partial",
        (
            "source_files_skipped",
            "source_rows_rejected",
            "selected_values_unknown",
            "independent_continuity_unverified",
        ),
    )


@pytest.mark.parametrize("output", [None, 0, 8])
def test_missing_exact_pin_not_backfilled_and_unknown_is_not_zero(output):
    view, env = _summarize(usage_summary.parse(_summary_doc(output)), measure="output")
    assert view["total"]["value"] == output
    assert env.source is None
    assert "missing_loaded_bytes" in env.coverage.reasons
    assert env.coverage.state == ("partial" if output is None else "unknown")
    assert selection_relation(env, env.scope) == SelectionRelation.UNSUPPORTED
    assert "missing_source" in pair_evidence(env, env).mismatches


def test_summary_rejects_projection_it_cannot_support():
    summary = usage_summary.parse(_summary_doc(0))
    with pytest.raises(ValueError, match="unsupported usage-summary measure"):
        summary_evidence(summary, measure="tokens_per_second", filters=(), unknown_values=0)
    with pytest.raises(ValueError, match="unsupported usage-summary filter"):
        summary_evidence(
            summary, measure="output", filters=(("account", "acct"),), unknown_values=0
        )


@pytest.mark.parametrize(
    ("actual", "requested", "result"),
    [
        (POPULATION, POPULATION, SelectionRelation.MATCHED),
        (POPULATION, Scope(POPULATION.population, (("model", "opus"),)), "independent_global"),
        (
            POPULATION,
            Scope("unrelated_population", (("provider", "example"),)),
            "independent_global",
        ),
        (Scope("p", (("provider", "a"),)), Scope("p", (("provider", "a"),)), "matched"),
        (Scope("p", (("provider", "a"),)), Scope("p", (("provider", "b"),)), "unsupported"),
        (Scope("p", (("provider", "a"),)), Scope("p"), "unsupported"),
        (
            Scope("p", (("account", "a"),), "registry-1"),
            Scope("p", (("account", "a"),), "registry-2"),
            "unsupported",
        ),
    ],
)
def test_global_never_inherits_selected_provider_account_or_population(actual, requested, result):
    evidence = replace(OBSERVED, scope=actual)
    assert selection_relation(evidence, requested) == result
    assert evidence.scope is actual


def test_pair_preserves_accepted_artifact_and_independent_proof_for_each_operand():
    expected = replace(OBSERVED, coverage=Coverage("complete", refs=(COVERAGE_PROOF,)))
    observed = replace(
        OBSERVED,
        source=SourceRef("independent_marginal", "d" * 64, GENERATION),
        coverage=Coverage("sampled", ("bounded_sample",), (COVERAGE_PROOF,)),
    )
    pair = pair_evidence(expected, observed)
    assert pair.compatible
    assert pair.expected is expected and pair.observed is observed
    assert pair.expected.coverage.state == "complete"
    assert pair.observed.coverage.state == "sampled"
    assert pair.observed.coverage.refs == (COVERAGE_PROOF,)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"source": None}, "missing_source"),
        (
            {"source": replace(FACT, generation=replace(GENERATION, id="other"))},
            "generation_mismatch",
        ),
        (
            {"source": replace(FACT, generation=replace(GENERATION, authority="rogue"))},
            "generation_mismatch",
        ),
        (
            {"source": replace(FACT, generation=replace(GENERATION, manifest_sha256="f" * 64))},
            "generation_mismatch",
        ),
        ({"source": replace(FACT, loaded_sha256="d" * 64)}, "loaded_bytes_mismatch"),
        ({"source": replace(FACT, kind="relabelled_bytes")}, "source_kind_mismatch"),
        ({"window": None}, "window_mismatch"),
        ({"window": replace(WINDOW, grain_seconds=None)}, "window_mismatch"),
        ({"window": replace(WINDOW, start=WINDOW.end, end=WINDOW.end + MINUTE)}, "window_mismatch"),
        ({"scope": None}, "scope_mismatch"),
        ({"scope": Scope(POPULATION.population, (("model", "a"),))}, "scope_mismatch"),
        ({"scope": Scope("another_population")}, "scope_mismatch"),
        ({"measure": "attempts"}, "measure_mismatch"),
        ({"unit": "seconds"}, "unit_mismatch"),
        ({"unit": None}, "unit_mismatch"),
        (
            {"coverage": Coverage("missing", ("requested_interval_not_published",))},
            "source_unavailable",
        ),
        ({"coverage": Coverage("rejected", ("row_identity_ambiguous",))}, "source_unavailable"),
    ],
)
def test_pair_refuses_incompatible_measurements_without_dropping_operand(change, reason):
    observed = replace(OBSERVED, **change)
    result = pair_evidence(OBSERVED, observed)
    assert not result.compatible and result.expected is OBSERVED and result.observed is observed
    assert result.mismatches == (reason,)
    if reason == "source_unavailable":
        assert selection_relation(observed, POPULATION) == SelectionRelation.UNSUPPORTED
        assert result.observed.coverage.reasons


def test_same_display_account_under_distinct_registries_is_not_an_identity_join():
    scope_a = Scope("proxy_accounts", (("account", "same-label"),), "registry-a")
    scope_b = replace(scope_a, identity_namespace="registry-b")
    pair = pair_evidence(replace(OBSERVED, scope=scope_a), replace(OBSERVED, scope=scope_b))
    assert pair.mismatches == ("scope_mismatch",)


def test_matching_unspecified_edges_do_not_align_different_sources():
    window = replace(WINDOW, grain_seconds=None, half_open=False)
    left = replace(OBSERVED, window=window)
    right = replace(left, source=SourceRef("another_kind", "d" * 64, GENERATION))
    assert pair_evidence(left, right).mismatches == ("unknown_edge_semantics",)


def test_gaps_are_exact_or_unlocalized_never_inferred_from_empty_data():
    gap = replace(WINDOW, end=START + MINUTE)
    partial = replace(
        OBSERVED, coverage=Coverage("partial", ("collector_gap",), (COVERAGE_PROOF,), (gap,))
    )
    pair = pair_evidence(OBSERVED, partial)
    assert pair.compatible
    assert pair.expected.coverage.gaps == () and pair.expected.coverage.state == "unknown"
    assert pair.observed.coverage.gaps == (gap,) and pair.observed.coverage.state == "partial"
    with pytest.raises(ValueError, match="localized gaps require evidence"):
        Coverage("partial", ("collector_gap",), gaps=(gap,))
    with pytest.raises(ValueError, match="UTC subranges"):
        replace(partial, coverage=replace(partial.coverage, gaps=(gap, gap)))
    outside = replace(gap, start=START - MINUTE)
    with pytest.raises(ValueError, match="UTC subranges"):
        replace(partial, coverage=replace(partial.coverage, gaps=(outside,)))


@pytest.mark.parametrize(
    "ref",
    [
        FACT,
        replace(COVERAGE_PROOF, generation=None),
        replace(COVERAGE_PROOF, loaded_sha256=FACT.loaded_sha256),
    ],
)
def test_complete_cannot_use_republished_bytes_or_proof_without_shared_generation(ref):
    with pytest.raises(ValueError, match="complete coverage requires independent proof"):
        Coverage("complete")
    with pytest.raises(ValueError, match="independent and in the same generation"):
        replace(OBSERVED, coverage=Coverage("complete", refs=(ref,)))


def test_utc_normalization_and_grain_not_inferred():
    brt = timezone(timedelta(hours=-3))
    assert Window(START.astimezone(brt), WINDOW.end.astimezone(brt), 60, True) == WINDOW
    with pytest.raises(ValueError, match="explicit time zones"):
        Window(START.replace(tzinfo=None), WINDOW.end)
    with pytest.raises(ValueError, match="not aligned"):
        replace(WINDOW, start=START + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="half-open edges"):
        replace(WINDOW, half_open=False)
    with pytest.raises(ValueError, match="invalid evidence window order"):
        replace(WINDOW, start=WINDOW.end)
