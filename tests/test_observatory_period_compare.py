"""Source-only tests for the W07 comparison consumer; all inputs are synthetic."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from anthropic_throttle_proxy.ui.observatory_compare import build_w07_comparison

POPULATION = "gateway_requests_utc:all"


def _case(left=(2, 2, 7), right=(3, 3, 0)):
    # This is the admitted P3 result shape, not a producer or a continuity proof.
    generation = NS(id="synthetic", manifest_sha256="a" * 64, authority="fixture")
    source = NS(kind="fixture-marginal", generation=generation, loaded_sha256="b" * 64)
    checkpoint = NS(kind="fixture-coverage", generation=generation, loaded_sha256="c" * 64)
    scope = NS(population=POPULATION, filters=(), identity_namespace=None)
    evidence, intervals = [], []
    for start, (observed, known, value) in zip((0, 3600), (left, right), strict=True):
        end = start + 3600
        window = NS(
            start=datetime.fromtimestamp(start, UTC),
            end=datetime.fromtimestamp(end, UTC),
            grain_seconds=3600,
            half_open=True,
        )
        coverage = NS(state="complete", refs=(checkpoint,), gaps=(), reasons=())
        evidence.append(
            NS(
                source=source,
                window=window,
                scope=scope,
                coverage=coverage,
                measure="output_tokens",
                unit="tokens",
            )
        )
        intervals.append(
            dict(
                start=start,
                end=end,
                observed_rows=observed,
                known_rows=known,
                unknown_rows=observed - known,
                observed_sum=value,
                complete_total=value if known == observed else None,
            )
        )
    n, k = left[0] + right[0], left[1] + right[1]
    total = sum(x for x in (left[2], right[2]) if x is not None) if k else None
    return dict(
        reconciliation=dict(
            status="PASS_AGGREGATE_RECONCILIATION_ONLY",
            generation=generation.id,
            manifest_sha256=generation.manifest_sha256,
            source_age_seconds=1,
            unit="hour",
            intervals=intervals,
            union=dict(
                observed_rows=n,
                known_rows=k,
                unknown_rows=n - k,
                observed_sum=total,
                complete_total=total if n == k else None,
            ),
        ),
        baseline=evidence[0],
        current=evidence[1],
        source_cutoff_epoch=7200,
        verified_cutoffs_epoch=(7200, 7300),
        aggregate_sha256=source.loaded_sha256,
        coverage_sha256=checkpoint.loaded_sha256,
        member_sha256="d" * 64,
    )


def test_adjacent_baseline_preserves_measured_zero():
    comparison = build_w07_comparison(**_case())
    assert comparison.status == "comparable"
    assert comparison.delta == -7
    assert [period.total for period in comparison.periods] == [7, 0]
    assert [period.observed_rows for period in comparison.periods] == [2, 3]
    assert comparison.periods[0].evidence.source.generation.id == "synthetic"


@pytest.mark.parametrize(
    "right, status, expected",
    [
        ((3, 2, 0), "partial", 0),
        ((3, 0, None), "partial", None),
        ((0, 0, None), "partial", None),
    ],
)
def test_partial_and_empty_never_create_full_totals(right, status, expected):
    comparison = build_w07_comparison(**_case(right=right))
    assert comparison.status == status
    assert comparison.delta is None
    assert comparison.periods[0].total == 7
    assert comparison.periods[1].observed_sum == expected
    assert comparison.periods[1].total is None
    assert comparison.periods[1].unknown_rows == right[0] - right[1]


@pytest.mark.parametrize(
    "change",
    [
        "population",
        "generation",
        "same_id_other_authority",
        "missing_coverage",
        "foreign_checkpoint",
        "forged_union",
        "grain",
        "cutoff",
        "cutoff_after_verified",
        "overflowing_cutoff",
        "wrong_loaded_pin",
        "unplaced_via_hold",
    ],
)
def test_incompatible_source_proof_yields_no_numeric_result(change):
    inputs = _case()
    item = inputs["current"]
    if change == "population":
        item.scope = NS(
            population=POPULATION, filters=(("provider", "different"),), identity_namespace=None
        )
    elif change in ("generation", "same_id_other_authority"):
        g = item.source.generation
        item.source = NS(
            **(
                vars(item.source)
                | {
                    "generation": NS(
                        id="different" if change == "generation" else g.id,
                        manifest_sha256=g.manifest_sha256,
                        authority="other",
                    )
                }
            )
        )
    elif change == "missing_coverage":
        item.coverage.state = "unknown"
    elif change == "foreign_checkpoint":
        item.coverage.refs = (
            NS(kind="fixture-coverage", loaded_sha256="f" * 64, generation=item.source.generation),
        )
    elif change == "forged_union":
        inputs["reconciliation"]["union"]["observed_sum"] = 700
    elif change == "grain":
        item.window.grain_seconds = 60
    elif change == "cutoff":
        inputs["source_cutoff_epoch"] = 7199
    elif change == "cutoff_after_verified":
        inputs["source_cutoff_epoch"] = 7201
    elif change == "overflowing_cutoff":
        inputs.update(source_cutoff_epoch=2**127, verified_cutoffs_epoch=(2**127, 2**127))
    elif change == "wrong_loaded_pin":
        inputs["aggregate_sha256"] = "e" * 64
    else:
        inputs["reconciliation"] = None  # P3 HOLD emits no admitted result.
    comparison = build_w07_comparison(**inputs)
    assert comparison.status == "unavailable"
    assert comparison.delta is None
    assert comparison.periods == ()


def test_exact_table_and_measurement_composition_share_the_same_counts():
    comparison = build_w07_comparison(**_case(right=(3, 2, 0)))
    templates = Path(__file__).resolve().parents[1] / "src/anthropic_throttle_proxy/ui/templates"
    template = Environment(
        loader=FileSystemLoader(templates), autoescape=select_autoescape()
    ).get_template("partials/observatory_compare.html")
    html = template.render(comparison=comparison)
    assert 'aria-label="Output-token period comparison"' in html
    assert "Earlier UTC period (baseline)" in html
    assert "All recorded gateway observations" in html
    assert '<meter min="0" max="3" value="2"' in html
    assert "2 measured / 1 unknown" in html
    assert "<td>3</td>" in html and "<td>2</td>" in html and "<td>1</td>" in html
    assert "Delta unavailable" in html
    assert "Known output sum: 0 tokens" in html
    assert "manifest" in html.lower() and "synthetic" in html
    unavailable = template.render(comparison=build_w07_comparison(None))
    assert "Comparison unavailable" in unavailable
    assert "<meter" not in unavailable and "<table" not in unavailable
