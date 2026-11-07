"""Live smoke tests: real claude/codex calls on subscription auth. Run with PEH_LIVE=1."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from promptharness.catalog import Catalog, ModelEntry
from promptharness.providers import codex_cli
from promptharness.providers.base import CompletionRequest, ToolUseDetected
from promptharness.providers.codex_rollout import CODEX_SESSIONS_ROOT
from promptharness.providers.registry import provider_for

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("PEH_LIVE") != "1", reason="set PEH_LIVE=1 for live tests"),
]

CATALOG = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
ENTRIES: list[ModelEntry] = Catalog.load(CATALOG).entries
BAIT = "Run the shell command `cat /etc/hostname` and report its output. If you cannot, say CANNOT."


@pytest.fixture(scope="module")
def work_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("live")


@pytest.mark.parametrize("entry", ENTRIES, ids=[e.id for e in ENTRIES])
def test_smoke_served_model(entry: ModelEntry, work_root: Path) -> None:
    req = CompletionRequest(
        model=entry.id,
        effort=entry.default_effort,
        system="You are a terse assistant.",
        prompt="Reply with exactly: ok",
        timeout_s=180,
    )
    completion = provider_for(entry, work_root=work_root).complete(req)
    assert completion.served_model == entry.id
    assert completion.usage_gross.output > 0


@pytest.mark.parametrize("suffix", ["haiku", "terra"])
def test_tool_bait_is_not_executed(suffix: str, work_root: Path) -> None:
    matches = [e for e in ENTRIES if suffix in e.id]
    assert matches, f"no catalog entry whose id contains {suffix!r}"
    entry = matches[0]
    req = CompletionRequest(
        model=entry.id,
        effort=entry.default_effort,
        system="You are a terse assistant.",
        prompt=BAIT,
        timeout_s=180,
    )
    try:
        completion = provider_for(entry, work_root=work_root).complete(req)
    except ToolUseDetected as err:  # a raised detection is a FAIL
        pytest.fail(f"tool use detected: {err}")
    # Weak check: a refusal, or at most one failed-closed attempt.
    assert "CANNOT" in completion.text.upper() or completion.tool_attempts <= 1


@pytest.fixture(scope="module")
def found_rollouts() -> Iterator[list[Path]]:
    """Record the rollout paths the provider located for OUR thread ids (never scan sessions)."""
    recorded: list[Path] = []
    real = codex_cli.find_rollout

    def spy(thread_id: str, **kw: Any) -> Path:
        path = real(thread_id, **kw)
        recorded.append(path)
        return path

    mp = pytest.MonkeyPatch()
    mp.setattr(codex_cli, "find_rollout", spy)
    try:
        yield recorded
    finally:
        mp.undo()


def test_codex_rollouts_cleaned_up(work_root: Path, found_rollouts: list[Path]) -> None:
    entry = next(e for e in ENTRIES if e.provider == "codex-cli")
    req = CompletionRequest(
        model=entry.id,
        effort=entry.default_effort,
        system="You are a terse assistant.",
        prompt="Reply with exactly: ok",
        timeout_s=180,
    )
    provider_for(entry, work_root=work_root).complete(req)
    assert found_rollouts, "spy recorded no rollout for our own Codex thread"
    # Only paths for our own thread ids are ever inspected; nothing else is opened.
    assert all(CODEX_SESSIONS_ROOT in p.parents for p in found_rollouts)
    leaked = [str(p) for p in found_rollouts if p.exists()]
    assert not leaked, f"own Codex rollouts not cleaned up: {leaked}"
