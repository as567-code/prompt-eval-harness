"""Maps catalog entries to provider instances."""

from __future__ import annotations

from pathlib import Path

from promptharness.catalog import ModelEntry
from promptharness.providers.base import Provider
from promptharness.providers.claude_cli import ClaudeCLIProvider
from promptharness.providers.codex_cli import CodexCLIProvider


def provider_for(entry: ModelEntry, *, work_root: Path) -> Provider:
    if entry.provider == "claude-cli":
        return ClaudeCLIProvider(work_root=work_root)
    if entry.provider == "codex-cli":
        return CodexCLIProvider(work_root=work_root)
    raise ValueError(f"unknown provider {entry.provider!r}")  # pragma: no cover
