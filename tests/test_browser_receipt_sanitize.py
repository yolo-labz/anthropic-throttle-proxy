"""The browser receipt must not retain UI account identifiers."""

import pytest
from check_gauge_wire import sanitize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("account+qa@example.test", "[account]"),
        ("lane a1b2c3d4 unavailable", "lane [bearer] unavailable"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJxYSJ9.signature", "[token]"),
        ("$168/mo", "[billing]/mo"),
        ("120 output tokens per second", "120 output tokens per second"),
    ],
)
def test_browser_receipt_redacts_identifiers(raw, expected):
    assert sanitize(raw) == expected
