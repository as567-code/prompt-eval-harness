"""Command-line entry point for promptharness."""

import typer

from promptharness import __version__
from promptharness.models_cli import models_app
from promptharness.sandbox_cli import sandbox_app
from promptharness.security import install_redaction

app = typer.Typer(help="Prompt-evaluation harness and model router.", no_args_is_help=True)
app.add_typer(models_app, name="models")
app.add_typer(sandbox_app, name="sandbox")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"promptharness {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Prompt-evaluation harness and model router."""
    install_redaction()
