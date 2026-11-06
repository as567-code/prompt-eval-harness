from pathlib import Path

import pytest

from promptharness.catalog import Catalog
from promptharness.cost import notional_cost_usd
from promptharness.usage import Usage

CATALOG = Catalog.load(Path(__file__).parents[2] / "config" / "models.yaml")


def test_opus_reference_cost() -> None:
    cost = notional_cost_usd(Usage(input=1500, output=4000), CATALOG.get("claude-opus-5-5"))
    assert cost == pytest.approx(0.086)


def test_claude_cli_parity() -> None:
    usage = Usage(input=2, cache_write_1h=686, output=1037)
    cost = notional_cost_usd(usage, CATALOG.get("claude-opus-5-5"))
    assert cost == pytest.approx(0.026236, rel=1e-6)


def test_haiku_cli_parity() -> None:
    usage = Usage(input=2, cache_write_1h=657, output=4)
    cost = notional_cost_usd(usage, CATALOG.get("claude-haiku-5-5"))
    assert cost == pytest.approx(0.0001336, rel=1e-6)


def test_haiku_long_context() -> None:
    cost = notional_cost_usd(Usage(input=100_001, output=0), CATALOG.get("claude-haiku-5-5"))
    assert cost == pytest.approx(100_001 * 0.50 / 1e6)


def test_haiku_at_threshold_is_not_long_context() -> None:
    cost = notional_cost_usd(Usage(input=100_000), CATALOG.get("claude-haiku-5-5"))
    assert cost == pytest.approx(100_000 * 0.10 / 1e6)


def test_openai_long_context() -> None:
    cost = notional_cost_usd(Usage(input=272_001), CATALOG.get("gpt-6.1-terra"))
    assert cost == pytest.approx(272_001 * 2.00 / 1e6)


def test_reasoning_not_priced_separately() -> None:
    entry = CATALOG.get("claude-opus-5-5")
    assert notional_cost_usd(Usage(output=100, reasoning=60), entry) == notional_cost_usd(
        Usage(output=100), entry
    )


def test_net_of_order() -> None:
    assert Usage(input=100, cache_read=50, cache_write_1h=30).net_of(120) == Usage(input=60)


def test_net_of_floor() -> None:
    assert Usage(input=5).net_of(10) == Usage()


def test_input_side() -> None:
    usage = Usage(input=1, cache_read=2, cache_write_5m=3, cache_write_1h=4, output=99)
    assert usage.input_side == 10
