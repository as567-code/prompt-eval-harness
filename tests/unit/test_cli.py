from typer.testing import CliRunner

from promptharness import __version__
from promptharness.cli import app


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"promptharness {__version__}"
