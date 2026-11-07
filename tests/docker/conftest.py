"""Session-level leak check for the docker tests."""

import subprocess
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def _assert_no_leaked_containers() -> Iterator[None]:
    yield
    ps = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", "name=peh-"],  # noqa: S607
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    if ps.returncode == 0:
        assert ps.stdout.strip() == "", f"leaked peh- containers: {ps.stdout!r}"
