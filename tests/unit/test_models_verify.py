from pathlib import Path

import pytest
from typer.testing import CliRunner

import promptharness.models_cli as models_cli
from promptharness.catalog import Catalog, ModelEntry
from promptharness.cli import app
from promptharness.models_cli import verify_models
from promptharness.providers.base import Provider, ServedModelMismatch
from promptharness.providers.claude_cli import ClaudeCLIProvider
from promptharness.providers.codex_cli import CodexCLIProvider
from promptharness.providers.fake import FakeProvider
from promptharness.providers.registry import provider_for

CATALOG = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def _entries() -> list[ModelEntry]:
    return Catalog.load(CATALOG).by_role("pool")[:2]


def test_verify_refuses_without_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEH_LIVE", raising=False)
    result = CliRunner().invoke(app, ["models", "verify", "--catalog", str(CATALOG)])
    assert result.exit_code == 2
    assert "refusing: set PEH_LIVE=1 to make live CLI calls" in result.output


def test_verify_all_ok() -> None:
    entries = _entries()

    def factory(entry: ModelEntry) -> Provider:
        return FakeProvider([FakeProvider.text("ok", model=entry.id)])

    rows = verify_models(entries, factory)
    assert [r.status for r in rows] == ["OK"] * len(entries)
    assert [r.model for r in rows] == [e.id for e in entries]
    assert rows[0].served == entries[0].id
    assert rows[0].output == 10
    assert rows[0].cli_version == "fake"
    assert rows[0].notional_usd > 0
    assert rows[0].quota_pressure is None


def test_verify_reports_mismatch() -> None:
    entries = _entries()

    def factory(entry: ModelEntry) -> Provider:
        return FakeProvider([ServedModelMismatch(entry.id, "other-model")])

    rows = verify_models(entries, factory)
    assert [r.status for r in rows] == ["FAIL"] * len(entries)
    assert rows[0].detail.startswith("served_model_mismatch: ")


def test_verify_flags_wrong_served_model() -> None:
    entries = _entries()[:1]
    rows = verify_models(
        entries, lambda e: FakeProvider([FakeProvider.text("ok", model="something-else")])
    )
    assert rows[0].status == "FAIL"


def test_cli_exit_codes_and_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PEH_LIVE", "1")
    monkeypatch.setattr(
        models_cli,
        "provider_for",
        lambda entry, *, work_root: FakeProvider([FakeProvider.text("ok", model=entry.id)]),
    )
    target = _entries()[0].id
    work = tmp_path / "w" / "cwd"
    args = ["models", "verify", "--catalog", str(CATALOG), "--model", target, "--work-root"]
    ok = CliRunner().invoke(app, [*args, str(work)])
    assert ok.exit_code == 0, ok.output
    assert "OK" in ok.output and target in ok.output
    assert work.is_dir()

    monkeypatch.setattr(
        models_cli,
        "provider_for",
        lambda entry, *, work_root: FakeProvider([ServedModelMismatch(entry.id, "x")]),
    )
    bad = CliRunner().invoke(app, [*args, str(tmp_path / "w2")])
    assert bad.exit_code == 1
    assert "FAIL" in bad.output and "served_model_mismatch" in bad.output


def test_cli_unknown_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PEH_LIVE", "1")
    result = CliRunner().invoke(
        app,
        [
            "models",
            "verify",
            "--catalog",
            str(CATALOG),
            "--model",
            "nope",
            "--work-root",
            str(tmp_path / "w"),
        ],
    )
    assert result.exit_code == 2


def test_provider_for_maps_providers(tmp_path: Path) -> None:
    cat = Catalog.load(CATALOG)
    claude = next(e for e in cat.by_role("pool") if e.provider == "claude-cli")
    codex = next(e for e in cat.by_role("pool") if e.provider == "codex-cli")
    assert isinstance(provider_for(claude, work_root=tmp_path), ClaudeCLIProvider)
    assert isinstance(provider_for(codex, work_root=tmp_path), CodexCLIProvider)
