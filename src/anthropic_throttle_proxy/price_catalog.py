"""Pinned token estimates; public tariff evidence is not account attribution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from typing import Literal

_BASE_KINDS = frozenset(("input", "output", "cache_read"))
_WRITE_KINDS = frozenset(("cache_write_5m", "cache_write_1h"))
_UNIT_EXPONENT = {1: 0, 1_000: -3, 1_000_000: -6}


@dataclass(frozen=True, slots=True)
class PriceCard:
    """One exact provider/model/plan, in one currency, over [start, end).

    Time bounds must be UTC. An absent token class or a rate of None is unpriced.
    An explicit Decimal(0) is a known free rate, not a missing rate.
    """

    provider: str
    model: str
    plan: str
    effective_from: datetime
    effective_until: datetime | None
    source_url: str
    currency: str
    unit_tokens: int
    rates: tuple[tuple[str, Decimal | None], ...]

    def __post_init__(self) -> None:
        end = self.effective_until
        if self.effective_from.tzinfo is not UTC or (
            end is not None and (end.tzinfo is not UTC or end <= self.effective_from)
        ):
            raise ValueError("price interval must be nonempty and use UTC")
        if self.unit_tokens not in _UNIT_EXPONENT or type(self.unit_tokens) is not int:
            raise ValueError("price unit must be 1, 1000 or 1000000 tokens")
        if len(self.currency) != 3 or not (self.currency.isascii() and self.currency.isalpha()):
            raise ValueError("price currency must be a three-letter code")
        if self.currency != self.currency.upper():
            raise ValueError("price currency must be uppercase")
        if not all((self.provider, self.model, self.plan, self.source_url)):
            raise ValueError("price card needs an exact scope and source")
        if len(dict(self.rates)) != len(self.rates):
            raise ValueError("duplicate token classes in price card")
        if any(
            rate is not None and (not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0)
            for _, rate in self.rates
        ):
            raise ValueError("rates must be finite, nonnegative decimals or None")

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.provider, self.model, self.plan


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    catalog_version: str
    currency: str
    amount: Decimal | None = None
    price_card: PriceCard | None = None
    unpriced_kinds: tuple[str, ...] = ()

    @property
    def status(self) -> Literal["estimated", "unknown"]:
        return "unknown" if self.amount is None else "estimated"


@dataclass(frozen=True, slots=True)
class PriceCatalog:
    version: str
    cards: tuple[PriceCard, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("catalog version is required")
        last_by_scope: dict[tuple[str, str, str], PriceCard] = {}
        for card in sorted(self.cards, key=lambda item: item.effective_from):
            previous = last_by_scope.get(card.scope)
            if previous is not None and (
                previous.effective_until is None or card.effective_from < previous.effective_until
            ):
                raise ValueError("overlapping price intervals for one scope")
            last_by_scope[card.scope] = card

    def estimate(
        self,
        *,
        provider: str | None,
        model: str | None,
        plan: str | None,
        at: datetime | None,
        usage: Mapping[str, int] | None,
        currency: str = "USD",
    ) -> TokenEstimate:
        """Price complete, disjoint token counts at usage time. Never convert FX.

        The caller must resolve the exact per-usage plan, including routing and
        request modifiers, and attest completeness before passing counts. Pass
        None for unadmitted scope, time or usage; never derive the plan from a
        model label. Input, output and cache-read counts are required (zero is
        explicit); omitted write counts assert no usage in that class. Do not
        pass the aggregate cache_creation count alongside either TTL breakdown.
        """
        unknown = TokenEstimate(self.version, currency)
        if at is None or at.utcoffset() is None:
            return unknown
        current = next(
            (
                card
                for card in self.cards
                if card.scope == (provider, model, plan)
                and card.effective_from <= at
                and (card.effective_until is None or at < card.effective_until)
            ),
            None,
        )
        if current is None or current.currency != currency or not _complete_usage(usage):
            return unknown
        assert usage is not None
        rates = dict(current.rates)
        unpriced = tuple(
            sorted(kind for kind, count in usage.items() if count and rates.get(kind) is None)
        )
        if unpriced:
            return TokenEstimate(
                self.version, currency, price_card=current, unpriced_kinds=unpriced
            )
        with localcontext(Context(prec=50)):
            quantity = sum(
                (rate * usage.get(kind, 0) for kind, rate in current.rates if rate is not None),
                Decimal(0),
            )
            amount = quantity.scaleb(_UNIT_EXPONENT[current.unit_tokens])
        return TokenEstimate(self.version, currency, amount, current)


def _complete_usage(usage: Mapping[str, int] | None) -> bool:
    if usage is None or not _BASE_KINDS <= usage.keys():
        return False
    if any(type(count) is not int or count < 0 for count in usage.values()):
        return False
    return "cache_creation" not in usage or not (_WRITE_KINDS & usage.keys())


# This cutoff is the catalog's conservative coverage start (UTC readback), not
# an asserted historical provider price-change instant. Older usage is unknown.
_PUBLIC_COVERAGE_START = datetime(2026, 11, 28, 16, 29, 13, tzinfo=UTC)
_PUBLIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
_PUBLIC_KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
PUBLIC_PRICE_CATALOG = PriceCatalog(
    "anthropic-standard-global-20261128-v1",
    tuple(
        PriceCard(
            provider="anthropic",
            model=model,
            plan="standard-api-global",
            effective_from=_PUBLIC_COVERAGE_START,
            effective_until=None,
            source_url=_PUBLIC_SOURCE,
            currency="USD",
            unit_tokens=1_000_000,
            rates=tuple(zip(_PUBLIC_KINDS, map(Decimal, rates), strict=True)),
        )
        for model, rates in (
            ("claude-fable-5-2", ("8", "40", "0.20", "10", "16")),
            ("claude-opus-5-5", ("4", "20", "0.20", "5", "8")),
            ("claude-sonnet-5-5", ("2", "10", "0.10", "2.50", "4")),
        )
    ),
)
