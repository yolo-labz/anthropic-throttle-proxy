"""Synthetic self-check of the independent oracle, not deployed acceptance."""

import json
from datetime import UTC, datetime

import pytest
from live_journal_oracle import compatible_rates, events, window


def test_exact_common_window_and_allowed_cached_endpoints(tmp_path):
    path = tmp_path / "usage.jsonl"
    rows = [(900, 1), (940, 600), (970, 1200), (1000, 999)]
    path.write_text(
        "".join(
            json.dumps(
                {
                    "ts": datetime.fromtimestamp(ts, UTC).isoformat(),
                    "output": count,
                    "provider": "test",
                }
            )
            + "\n"
            for ts, count in rows
        )
    )
    observed, _, _ = events(path)
    assert window(observed, 1000) == (1800, ["test"], 2)
    assert "30" in compatible_rates(observed, 999, 1000)
    # Both simultaneous boundary changes occur just after1000: +999 and−600.
    assert compatible_rates(observed, 1000, 1000.1) == {"30", "37"}


def test_partial_real_data_cannot_fabricate_a_reference(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text('{"ts":')
    with pytest.raises(AssertionError, match="incomplete"):
        events(path)
