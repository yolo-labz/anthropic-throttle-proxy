"""Conformance at the frozen W17/N14 boundary, not new-view or served acceptance."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import pytest

from anthropic_throttle_proxy import usage_summary
from anthropic_throttle_proxy.ui import observatory, usage
from anthropic_throttle_proxy.ui import observatory_evidence as evidence


@pytest.fixture
def frozen_summary(tmp_path, monkeypatch):
    # No real rows or identity keys. Distinct bytes can describe exactly the same facts.
    document = {
        "schema": "usage-summary.v1",
        "generated_at": "2026-01-01T02:00:00Z",
        "coverage": {
            "window_start": "2026-01-01T00:00:00Z",
            "window_end": "2026-01-01T01:00:00Z",
            "files_scanned": 1,
            "files_skipped": 0,
        },
        "rows": [],
    }
    raw = json.dumps(document).encode()
    path = tmp_path / "summary.json"
    path.write_bytes(raw)
    monkeypatch.setenv(usage.ENV, str(path))
    monkeypatch.setattr(usage, "_cache", {"key": None, "result": (None, None)})
    return path, raw


def test_w17_and_n14_agree_on_consumed_bytes_not_publication_timestamp(frozen_summary):
    path, raw = frozen_summary
    metric = {"measure": "tokens"}
    digest = sha256(raw).hexdigest()
    pin_query = parse_qs(urlsplit(observatory.view(metric)["freeze_url"]).query)
    assert pin_query["as_of"] == [digest]
    assert observatory.view(metric, frozen_sha256=digest)["loaded_sha256"] == digest

    before = evidence.summary_evidence(
        usage_summary.load(path), measure="tokens", filters=(), unknown_values=0
    )
    assert before.source.loaded_sha256 == digest
    assert before.source.generation is None
    assert before.coverage.state == "unknown"
    assert not before.window.half_open

    # Identical metadata and values must not make rotated bytes pairable.
    rotated = path.with_suffix(".next")
    rotated.write_bytes(raw + b"\n")
    rotated.replace(path)
    current = observatory.view(metric)
    assert current["loaded_sha256"] == sha256(raw + b"\n").hexdigest()
    after = evidence.summary_evidence(
        usage_summary.load(path), measure="tokens", filters=(), unknown_values=0
    )
    assert before.window == after.window
    assert before.scope == after.scope
    assert (before.measure, before.unit) == (after.measure, after.unit)
    assert not evidence.pair_evidence(before, after).compatible


@pytest.fixture
def linked_evidence():
    """Only v1 operand shapes; these test-local pins aren't a production source."""
    generation = evidence.Generation("fixture-owner", "fixture-v1", "a" * 64)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, 1, tzinfo=UTC)
    window = evidence.Window(start, end, grain_seconds=60, half_open=True)
    scope = evidence.Scope("fixture-usage", (("account", "fixture-a"),), "fixture-registry-v1")
    coverage = evidence.Coverage("unknown", reasons=("fixture_continuity_unverified",))
    left = dict(
        source=evidence.SourceRef("fixture-chart", "b" * 64, generation),
        window=window,
        scope=scope,
        measure="output",
        unit="tokens",
        coverage=coverage,
    )
    right = left | {"source": evidence.SourceRef("fixture-table", "c" * 64, generation)}
    return left, right


@pytest.mark.parametrize("changed", ["unit", "manifest", "namespace", "window", "same-kind-bytes"])
def test_pair_rejects_one_dimension_changed_with_all_others_equal(linked_evidence, changed):
    left, right = linked_evidence
    expected = evidence.Evidence(**left)
    assert evidence.pair_evidence(expected, evidence.Evidence(**right)).compatible

    if changed == "unit":
        right["unit"] = "seconds"
    elif changed == "manifest":
        new = evidence.Generation("fixture-owner", "fixture-v1", "d" * 64)
        right["source"] = evidence.SourceRef("fixture-table", "c" * 64, new)
    elif changed == "namespace":
        right["scope"] = evidence.Scope(
            "fixture-usage", (("account", "fixture-a"),), "fixture-registry-v2"
        )
    elif changed == "window":
        start = datetime(2026, 1, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 2, tzinfo=UTC)
        right["window"] = evidence.Window(start, end, grain_seconds=60, half_open=True)
    else:
        # Sharing a manifest does not excuse different bytes of the same source kind.
        right["source"] = evidence.SourceRef("fixture-chart", "c" * 64, left["source"].generation)

    observed = evidence.Evidence(**right)
    result = evidence.pair_evidence(expected, observed)
    assert result.expected == expected and result.observed == observed
    assert not result.compatible and result.mismatches


def test_global_is_not_a_filtered_projection_even_with_same_population(linked_evidence):
    left, right = linked_evidence
    requested = left["scope"]
    actual = evidence.Scope(requested.population)
    global_evidence = evidence.Evidence(**(right | {"scope": actual}))
    assert evidence.selection_relation(evidence.Evidence(**left), requested) == "matched"
    assert evidence.selection_relation(global_evidence, requested) == "independent_global"
    assert global_evidence.scope == actual
    assert not evidence.pair_evidence(evidence.Evidence(**left), global_evidence).compatible


@pytest.mark.parametrize("unavailable", ["missing", "rejected"])
def test_unavailable_cannot_project_even_when_its_descriptors_match(linked_evidence, unavailable):
    left, right = linked_evidence
    right["coverage"] = evidence.Coverage(unavailable, reasons=("fixture_unavailable",))
    observed = evidence.Evidence(**right)
    assert evidence.selection_relation(observed, left["scope"]) == "unsupported"
    assert not evidence.pair_evidence(evidence.Evidence(**left), observed).compatible


def test_compatible_pair_retains_independently_evidenced_restart_gap(linked_evidence):
    left, right = linked_evidence
    start = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
    end = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    gap = evidence.Window(start, end, half_open=True)
    proof = evidence.SourceRef("fixture-checkpoint", "e" * 64, left["source"].generation)
    right["coverage"] = evidence.Coverage(
        "partial", reasons=("restart_gap",), refs=(proof,), gaps=(gap,)
    )
    pair = evidence.pair_evidence(evidence.Evidence(**left), evidence.Evidence(**right))
    assert pair.compatible  # Compatibility is not proof of complete coverage.
    assert pair.expected.coverage.state == "unknown"
    assert pair.observed.coverage.state == "partial"
    assert pair.observed.coverage.gaps == (gap,)
    assert pair.observed.coverage.refs == (proof,)
