"""Catalog and pricing seam contracts; the dated price correction is synthetic."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from anthropic_throttle_proxy.price_catalog import PUBLIC_PRICE_CATALOG, PriceCard, PriceCatalog

CHANGE = datetime(2026, 9, 1, tzinfo=UTC)
PUBLIC_START = datetime(2026, 11, 28, 16, 29, 13, tzinfo=UTC)
SCOPE = {"provider": "anthropic", "model": "claude-fable-5-2", "plan": "standard-api-global"}
COUNTS = {"input": 1000, "output": 500, "cache_read": 2500}


def _card(
    start: datetime,
    end: datetime | None,
    rate: str,
    *,
    currency: str = "USD",
    unit_tokens: int = 1_000,
) -> PriceCard:
    return PriceCard(
        provider="test-provider",
        model="test-model",
        plan="test-plan",
        effective_from=start,
        effective_until=end,
        source_url="https://example.test/price-change",
        currency=currency,
        unit_tokens=unit_tokens,
        rates=(("input", Decimal(rate)), ("output", Decimal("0"))),
    )


def _sample(catalog: PriceCatalog, at: datetime, currency: str = "USD"):
    return catalog.estimate(
        provider="test-provider",
        model="test-model",
        plan="test-plan",
        at=at,
        usage={"input": 2500, "output": 100, "cache_read": 0},
        currency=currency,
    )


def test_synthetic_rate_change_selects_new_rate_at_the_exclusive_boundary():
    old = _card(CHANGE - timedelta(days=1), CHANGE, "0.0011")
    new = _card(CHANGE, None, "0.0017")
    catalog = PriceCatalog("fixture-v1", (new, old))

    before = _sample(catalog, CHANGE - timedelta(microseconds=1))
    at = _sample(catalog, datetime(2026, 8, 31, 21, tzinfo=timezone(-timedelta(hours=3))))
    assert before.amount == Decimal("0.00275")
    assert before.price_card == old
    assert at.amount == Decimal("0.00425")
    assert at.price_card == new
    assert at.status == "estimated"
    assert at.catalog_version == "fixture-v1"


def test_an_expired_rate_does_not_bridge_a_gap():
    catalog = PriceCatalog("fixture", (_card(CHANGE - timedelta(days=1), CHANGE, "1"),))
    assert _sample(catalog, CHANGE).amount is None


def test_overlapping_effective_intervals_cannot_be_published():
    with pytest.raises(ValueError, match="overlapping"):
        PriceCatalog("fixture", (_card(CHANGE, None, "1"), _card(CHANGE, None, "2")))


def test_currency_must_match_the_card_no_implicit_fx():
    catalog = PriceCatalog("fixture", (_card(CHANGE, None, "0.01", currency="EUR", unit_tokens=1),))
    assert _sample(catalog, CHANGE, "EUR").amount == Decimal("25.00")
    assert _sample(catalog, CHANGE).amount is None


def test_verified_public_api_card_distinguishes_the_two_cache_write_ttls():
    result = PUBLIC_PRICE_CATALOG.estimate(
        **SCOPE,
        at=PUBLIC_START,
        usage=COUNTS | {"cache_write_5m": 1000, "cache_write_1h": 500},
    )
    assert result.amount == Decimal("0.0465")
    assert result.currency == "USD"
    assert result.catalog_version == "anthropic-standard-global-20261128-v1"
    assert result.price_card is not None
    assert result.price_card.effective_from == PUBLIC_START
    assert (
        result.price_card.source_url == "https://platform.claude.com/docs/en/about-claude/pricing"
    )
    assert result.price_card.unit_tokens == 1_000_000
    assert not hasattr(result, "billed_amount")


@pytest.mark.parametrize(
    ("model", "expected"),
    [("claude-opus-5-5", "0.0145"), ("claude-sonnet-5-5", "0.00725")],
)
def test_other_exact_provider_model_ids_match_the_published_usd_card(model, expected):
    result = PUBLIC_PRICE_CATALOG.estimate(
        **(SCOPE | {"model": model}), at=PUBLIC_START, usage=COUNTS
    )
    assert result.amount == Decimal(expected)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "unrecognized"},
        {"model": "anthropic/claude-fable-5-2"},
        {"model": "claude-fable-5-20"},
        {"model": "claude-haiku-5-5"},
        {"plan": "subscription"},
        {"plan": "batch"},
        {"plan": "custom"},
        {"plan": None},
        {"at": None},
        {"at": PUBLIC_START.replace(tzinfo=None)},
        {"at": PUBLIC_START - timedelta(microseconds=1)},
        {"usage": None},
        {"usage": {}},
        {"usage": {"output": 0}},
        {"usage": COUNTS | {"input": True}},
        {"usage": COUNTS | {"output": -1}},
        {"usage": COUNTS | {"audio": 1}},
        {"usage": COUNTS | {"cache_creation": 5}},
        {"usage": COUNTS | {"cache_creation": 5, "cache_write_5m": 5}},
        {"usage": COUNTS | {"cache_creation": 0, "cache_write_1h": 8}},
    ],
)
def test_unattested_scope_or_incomplete_usage_has_no_amount(overrides):
    estimate = PUBLIC_PRICE_CATALOG.estimate(
        **(SCOPE | {"at": PUBLIC_START, "usage": COUNTS} | overrides)
    )
    assert estimate.amount is None
    assert estimate.status == "unknown"
    assert estimate.catalog_version == "anthropic-standard-global-20261128-v1"


def test_missing_positive_token_rate_is_reported_without_showing_a_partial_amount():
    result = PUBLIC_PRICE_CATALOG.estimate(
        **SCOPE, at=PUBLIC_START, usage=COUNTS | {"cache_creation": 5}
    )
    assert result.amount is None
    assert result.unpriced_kinds == ("cache_creation",)
    assert result.price_card is not None


def test_explicit_zero_usage_under_a_known_rate_is_an_estimate_not_unknown():
    explicit_zeros = dict.fromkeys(COUNTS, 0) | {"cache_creation": 0}
    result = PUBLIC_PRICE_CATALOG.estimate(**SCOPE, at=PUBLIC_START, usage=explicit_zeros)
    assert result.amount == Decimal("0")
