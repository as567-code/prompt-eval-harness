"""Docker tests for the sandbox image (skipped when docker or sandbox/IMAGE_ID is absent)."""

import json
import subprocess
from pathlib import Path

import pytest

IMAGE_ID = Path(__file__).resolve().parents[2] / "sandbox" / "IMAGE_ID"

pytestmark = pytest.mark.docker


def test_image_user_and_entrypoint() -> None:
    out = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", IMAGE_ID.read_text(encoding="utf-8").strip()],  # noqa: S607
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    ).stdout
    config = json.loads(out)[0]["Config"]
    assert config["User"] == "10001:10001"
    assert config["Entrypoint"][-1].endswith("/opt/peh/runner.py")
