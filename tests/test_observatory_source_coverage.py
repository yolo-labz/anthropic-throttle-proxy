"""Known source omissions qualify the headline, not selected group attribution."""

import json
from pathlib import Path

import jinja2
import pytest

from anthropic_throttle_proxy import usage_summary
from anthropic_throttle_proxy.ui import observatory

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("counts", "skipped", "rejected", "expected", "headline", "group"),
    [
        ((11, 2, 3, 4), 2, False, 20, "≥ 20", "20"),
        ((11, 2, 3, 4), 0, False, 20, "20", "20"),
        ((0, 0, 0, 0), 0, False, 0, "0", "0"),
        ((0, 0, 0, 0), 2, False, 0, "≥ 0", "0"),
        ((None, None, None, None), 2, False, None, "unknown", "unknown"),
        ((None, None, None, None), 0, False, None, "unknown", "unknown"),
        ((11, 2, 3, 4), 0, True, 20, "≥ 20", "20"),
        ((1260, 0, 0, 0), 2, False, 1260, "≥ 1,260", "1.3k"),
    ],
    ids=[
        "skipped-only",
        "no-reported-omissions",
        "measured-zero",
        "observed-zero-incomplete-source",
        "unmeasured-incomplete-source",
        "unmeasured-no-reported-omissions",
        "rejected-only",
        "lower-bound-is-not-rounded-up",
    ],
)
def test_headline_separates_observed_sum_from_source_coverage(
    counts, skipped, rejected, expected, headline, group
):
    doc = json.loads((ROOT / "specs/380-usage-panel/usage-summary.v1.example.json").read_text())
    row = doc["rows"][0]
    row.update(zip(usage_summary.ADDITIVE_CLASSES, counts, strict=True))
    doc["coverage"]["files_skipped"] = skipped
    doc["rows"] = [row, {"provider": "invalid"}] if rejected else [row]
    source = usage_summary.parse(doc)
    view = observatory.build_view(
        source, {"provider": row["provider"]}, now=source.generated_at.timestamp()
    )
    assert view["files_skipped"] == skipped and view["rejected_rows"] == int(rejected)
    assert view["total"]["value"] == expected
    assert view["total"]["compact"] == group
    assert view["total"]["unknown"] == counts.count(None)
    assert view["matched_rows"] == len(view["table_rows"]) == 1
    assert view["providers"][0]["models"][0]["metric"]["compact"] == group
    assert view["totals"][1]["value"] == counts[0]
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "src/anthropic_throttle_proxy/ui/templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    html = env.get_template("observatory.html").render(
        ob=view,
        measures=observatory.MEASURES,
        roles=observatory.ROLES,
        columns=observatory.COLUMNS,
        asset_v="fixture",
    )
    assert f'<p class="ob-total">{headline} <span>tokens</span></p>' in html
    assert ("Source coverage has reported omissions." in html) == bool(skipped or rejected)
    if skipped or rejected:
        assert "cannot be assigned to the selected groups" in html
        assert ("The headline is a lower bound" in html) == (expected is not None)
    assert "No reported exclusions does not prove complete coverage." in html
