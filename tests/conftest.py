"""Shared pytest hooks: auto-skip docker-marked tests when the sandbox is unavailable."""

import subprocess
from pathlib import Path

import pytest

IMAGE_ID_FILE = Path(__file__).resolve().parents[1] / "sandbox" / "IMAGE_ID"


def _docker_skip_reason() -> str | None:
    try:
        info = subprocess.run(
            ["docker", "info"],  # noqa: S607
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker unavailable: {exc}"
    if info.returncode != 0:
        return "docker daemon unavailable (`docker info` failed)"
    if not IMAGE_ID_FILE.is_file() or not IMAGE_ID_FILE.read_text(encoding="utf-8").strip():
        return "sandbox/IMAGE_ID missing; run `promptharness sandbox build`"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    docker_items = [item for item in items if item.get_closest_marker("docker")]
    if not docker_items:
        return
    reason = _docker_skip_reason()
    if reason is None:
        return
    marker = pytest.mark.skip(reason=reason)
    for item in docker_items:
        item.add_marker(marker)
