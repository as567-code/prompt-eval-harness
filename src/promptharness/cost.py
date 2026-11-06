"""Notional cost: CLI-reported token usage times list prices."""

from __future__ import annotations

from promptharness.catalog import LongContextPrices, ModelEntry
from promptharness.usage import Usage

_PER_MILLION = 1_000_000


def notional_cost_usd(usage: Usage, entry: ModelEntry) -> float:
    """Price ``usage`` at ``entry``'s list prices (USD per 1M tokens).

    ``reasoning`` is already inside ``output`` and is not priced separately.
    """
    prices: ModelEntry | LongContextPrices = entry
    if (
        entry.long_context_threshold is not None
        and entry.long_context is not None
        and usage.input_side > entry.long_context_threshold
    ):
        prices = entry.long_context
    total = (
        usage.input * prices.input
        + usage.output * prices.output
        + usage.cache_read * prices.cache_read
        + usage.cache_write_5m * prices.cache_write_5m
        + usage.cache_write_1h * prices.cache_write_1h
    )
    return total / _PER_MILLION
