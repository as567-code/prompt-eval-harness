"""``promptharness sandbox`` sub-app: build the pinned sandbox image."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from promptharness.sandbox.image import ImageBuildError, build_image

sandbox_app = typer.Typer(help="Hardened Docker sandbox.", no_args_is_help=True)

_DEFAULT_CONTEXT = Path("sandbox")


@sandbox_app.command("build")
def build(
    context: Annotated[Path, typer.Option(help="Docker build context.")] = _DEFAULT_CONTEXT,
) -> None:
    """Build the sandbox image, record its ID in <context>/IMAGE_ID and print it."""
    try:
        image_id = build_image(context)
    except ImageBuildError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    (context / "IMAGE_ID").write_text(image_id + "\n", encoding="utf-8")
    typer.echo(image_id)
