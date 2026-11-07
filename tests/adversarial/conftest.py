"""Adversarial suite fixtures: shared runner and a per-module leaked-container check."""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from promptharness.sandbox.docker_runner import SandboxRunner

IMAGE_ID = Path(__file__).resolve().parents[2] / "sandbox" / "IMAGE_ID"


@pytest.fixture(scope="module")
def runner() -> SandboxRunner:
    return SandboxRunner(image_id=IMAGE_ID.read_text(encoding="utf-8").strip(), timeout_s=30)


def _leaked_containers() -> str | None:
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=peh-", "-q"],  # noqa: S607
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    return ps.stdout.strip() if ps.returncode == 0 else None


@pytest.fixture(scope="module", autouse=True)
def _no_leaked_containers() -> Iterator[None]:
    yield
    leaked = _leaked_containers()
    assert not leaked, f"leaked peh- containers: {leaked!r}"


@pytest.fixture
def leaked_containers() -> Callable[[], str | None]:
    return _leaked_containers
