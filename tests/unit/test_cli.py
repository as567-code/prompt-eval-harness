import logging
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from promptharness import __version__
from promptharness.cli import app


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"promptharness {__version__}"


@pytest.fixture
def restore_factory() -> Iterator[None]:
    original = logging.getLogRecordFactory()
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)


def test_cli_callback_installs_redaction(restore_factory: None) -> None:
    @app.command("peh-test-noop")
    def _noop() -> None:  # pragma: no cover - trivial
        return None

    try:
        logging.setLogRecordFactory(logging.LogRecord)
        result = CliRunner().invoke(app, ["peh-test-noop"])
        assert result.exit_code == 0
        assert getattr(logging.getLogRecordFactory(), "_peh_redacting", False)
    finally:
        app.registered_commands.pop()
