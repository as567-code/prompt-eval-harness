"""Model catalog loaded from ``config/models.yaml``."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from promptharness.security import ConfigError, load_yaml

Price = Field(ge=0)


class UnknownModelError(KeyError):
    """Raised when a model id is not in the catalog."""


class LongContextPrices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: float = Price
    output: float = Price
    cache_read: float = Price
    cache_write_5m: float = Price
    cache_write_1h: float = Price


class ModelEntry(BaseModel):
    """One catalog row. Prices are USD per 1M tokens."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Literal["claude-cli", "codex-cli"]
    family: Literal["anthropic", "openai"]
    roles: list[Literal["pool", "judge", "auditor", "generator"]]
    default_effort: Literal["low", "medium", "high", "xhigh", "max"]
    input: float = Price
    output: float = Price
    cache_read: float = Price
    cache_write_5m: float = Price
    cache_write_1h: float = Price
    long_context_threshold: int | None = Field(ge=0)
    long_context: LongContextPrices | None
    source: str
    verified_on: date

    @model_validator(mode="after")
    def _threshold_matches_prices(self) -> Self:
        if (self.long_context_threshold is None) != (self.long_context is None):
            raise ValueError("long_context_threshold and long_context must be set together")
        return self


class _CatalogFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelEntry]


class Catalog:
    def __init__(self, entries: list[ModelEntry]) -> None:
        self.entries = entries
        self._by_id: dict[str, ModelEntry] = {}
        for entry in entries:
            if entry.id in self._by_id:
                raise ConfigError(f"duplicate model id: {entry.id}")
            self._by_id[entry.id] = entry

    @classmethod
    def load(cls, path: Path) -> Catalog:
        raw = load_yaml(path)
        try:
            parsed = _CatalogFile.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{path}: invalid model catalog: {exc}") from exc
        return cls(parsed.models)

    def get(self, model_id: str) -> ModelEntry:
        try:
            return self._by_id[model_id]
        except KeyError:
            raise UnknownModelError(model_id) from None

    def by_role(self, role: str) -> list[ModelEntry]:
        return [e for e in self.entries if role in e.roles]
