"""Build the pinned sandbox image and resolve its immutable image ID."""

from __future__ import annotations

import subprocess
from pathlib import Path

IMAGE_TAG = "promptharness-sandbox:0.1"


class ImageBuildError(RuntimeError):
    """Raised when ``docker build`` or ``docker image inspect`` fails."""


def build_image(context: Path) -> str:
    """Build ``IMAGE_TAG`` from ``context`` and return the image ID (``sha256:...``)."""
    build = subprocess.run(  # noqa: S603
        ["docker", "build", "-t", IMAGE_TAG, str(context)],  # noqa: S607
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if build.returncode != 0:
        raise ImageBuildError(f"docker build failed with exit code {build.returncode}")
    inspect = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE_TAG],  # noqa: S607
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    image_id = inspect.stdout.strip()
    if inspect.returncode != 0 or not image_id:
        raise ImageBuildError(f"docker image inspect failed: {inspect.stderr.strip()}")
    return image_id
