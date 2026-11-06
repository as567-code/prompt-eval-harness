from pathlib import Path

import pytest

from promptharness.catalog import Catalog, UnknownModelError
from promptharness.security import ConfigError

PATH = Path(__file__).parents[2] / "config" / "models.yaml"


def test_catalog_unknown_model() -> None:
    with pytest.raises(UnknownModelError):
        Catalog.load(PATH).get("nope")


def test_catalog_roles() -> None:
    ids = {e.id for e in Catalog.load(PATH).by_role("judge")}
    assert ids == {"claude-sonnet-5-5", "gpt-6.1-sol"}


def test_catalog_other_roles() -> None:
    cat = Catalog.load(PATH)
    assert {e.id for e in cat.by_role("pool")} == {
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "gpt-6-luna",
        "gpt-6.1-terra",
        "gpt-6.1-sol",
    }
    assert [e.id for e in cat.by_role("auditor")] == ["claude-fable-5-1"]
    assert [e.id for e in cat.by_role("generator")] == ["claude-opus-5-5"]


def test_default_efforts() -> None:
    cat = Catalog.load(PATH)
    assert cat.get("claude-sonnet-5-5").default_effort == "high"
    assert cat.get("claude-fable-5-1").default_effort == "high"
    assert cat.get("claude-opus-5-5").default_effort == "medium"
    assert cat.get("gpt-6.1-sol").default_effort == "medium"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "m.yaml"
    p.write_text(text)
    return p


ENTRY = """models:
  - id: x
    provider: claude-cli
    family: anthropic
    roles: [pool]
    default_effort: high
    input: 1
    output: 1
    cache_read: 1
    cache_write_5m: 1
    cache_write_1h: 1
    long_context_threshold: null
    long_context: null
    source: s
    verified_on: 2026-11-06
"""


def test_valid_entry_loads(tmp_path: Path) -> None:
    assert Catalog.load(_write(tmp_path, ENTRY)).get("x").input == 1


def test_unknown_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Catalog.load(_write(tmp_path, ENTRY + "    bogus: 1\n"))


def test_negative_price_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Catalog.load(_write(tmp_path, ENTRY.replace("input: 1", "input: -1")))


def test_negative_long_context_price_rejected(tmp_path: Path) -> None:
    bad = ENTRY.replace(
        "long_context: null",
        "long_context: {input: 1, output: -1, cache_read: 1, cache_write_5m: 1, cache_write_1h: 1}",
    )
    with pytest.raises(ConfigError):
        Catalog.load(_write(tmp_path, bad))


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    dup = ENTRY + ENTRY.split("models:\n", 1)[1]
    with pytest.raises(ConfigError):
        Catalog.load(_write(tmp_path, dup))


def test_threshold_without_prices_rejected(tmp_path: Path) -> None:
    bad = ENTRY.replace("long_context_threshold: null", "long_context_threshold: 10")
    with pytest.raises(ConfigError):
        Catalog.load(_write(tmp_path, bad))


def test_shipped_catalog_has_seven_entries() -> None:
    assert len(Catalog.load(PATH).entries) == 7
