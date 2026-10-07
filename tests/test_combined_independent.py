"""Keep the independent public journal oracle in normal current-head CI."""

from check_combined_accounting import cache_checks, checks


def test_independent_completion_window():
    assert len(checks()["passed"]) >= 12


async def test_independent_cache_projection_and_replacement():
    assert len(await cache_checks()) == 2
