"""End-to-end SandboxRunner tests against the built image (auto-skipped without docker)."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from promptharness.sandbox.docker_runner import SandboxResult, SandboxRunner, SandboxStatus

IMAGE_ID = Path(__file__).resolve().parents[2] / "sandbox" / "IMAGE_ID"

pytestmark = pytest.mark.docker

GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
TESTS = (
    "from solution import add\n\n"
    "def test_one():\n    assert add(1, 2) == 3\n\n"
    "def test_two():\n    assert add(0, 0) == 0\n\n"
    "def test_three():\n    assert add(-1, 1) == 0\n"
)


@pytest.fixture(scope="module")
def runner() -> SandboxRunner:
    return SandboxRunner(image_id=IMAGE_ID.read_text(encoding="utf-8").strip(), timeout_s=60)


def _run(runner: SandboxRunner, solution: str, tests: str = TESTS) -> SandboxResult:
    return runner.run(
        {"solution.py": solution, "test_hidden.py": tests}, test_files=["test_hidden.py"]
    )


def test_passing_solution(runner: SandboxRunner) -> None:
    res = _run(runner, GOOD)
    assert res.status is SandboxStatus.PASSED, res
    assert res.passed == res.total == 3


def test_failing_solution(runner: SandboxRunner) -> None:
    res = _run(runner, BAD)
    assert res.status is SandboxStatus.FAILED, res
    assert res.total == 3
    assert res.passed < 3


def test_syntax_error_solution_is_failed_not_tamper(runner: SandboxRunner) -> None:
    res = _run(runner, "def add(:\n")
    assert res.status is SandboxStatus.FAILED, res


def test_syntax_error_candidate_is_failed_with_info(runner: SandboxRunner) -> None:
    from promptharness.sandbox.tamper import scan

    bad = "def add(:\n"
    assert "syntax_error" in scan(bad).info
    res = runner.run(
        {"solution.py": bad, "test_hidden.py": TESTS},
        test_files=["test_hidden.py"],
        candidate=bad,
    )
    assert res.status is SandboxStatus.FAILED, res


def test_missing_test_is_tamper(runner: SandboxRunner) -> None:
    # Candidate-supplied conftest silently deselects one hidden test and reports success.
    conftest = (
        "def pytest_collection_modifyitems(config, items):\n"
        "    items[:] = [i for i in items if i.name != 'test_two']\n"
    )
    res = runner.run(
        {"solution.py": GOOD, "test_hidden.py": TESTS, "conftest.py": conftest},
        test_files=["test_hidden.py"],
    )
    assert res.status is SandboxStatus.TAMPER_SUSPECT, res


def test_parallel_runs_are_isolated(runner: SandboxRunner) -> None:
    sols = [GOOD, BAD, GOOD, BAD]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda s: _run(runner, s), sols))
    assert [r.status for r in results] == [
        SandboxStatus.PASSED,
        SandboxStatus.FAILED,
        SandboxStatus.PASSED,
        SandboxStatus.FAILED,
    ]


def test_binary_garbage_stdout(runner: SandboxRunner) -> None:
    sol = GOOD + "import sys\nsys.stdout.buffer.write(b'\\xff' * 1000)\nsys.stdout.buffer.flush()\n"
    res = _run(runner, sol)
    assert res.status is not SandboxStatus.SANDBOX_ERROR, res
    assert res.status is SandboxStatus.PASSED


def test_os_exit_zero_at_import_is_failed_no_report(runner: SandboxRunner) -> None:
    res = _run(runner, "import os\nos._exit(0)\n")
    assert res.status is SandboxStatus.FAILED, res
    assert res.detail.startswith("no report")


def test_forged_envelope_print_does_not_pass(runner: SandboxRunner) -> None:
    forged = (
        '{"v":1,"exit_code":0,"junit_xml":"<testsuites/>","stdout_tail":"",'
        '"stderr_tail":"","error":null}'
    )
    sol = (
        f"import sys, os\nprint({forged!r})\nsys.stdout.flush()\n"
        f"os.write(1, ({forged!r}+'\\n').encode())\n" + BAD
    )
    res = _run(runner, sol)
    assert res.status is SandboxStatus.FAILED, res
    assert res.status is not SandboxStatus.PASSED


def test_host_timeout_kills_container(runner: SandboxRunner) -> None:
    fast = SandboxRunner(image_id=runner._image_id, timeout_s=8, grace_s=0.5)
    # Inner timeout is max(1, 8-5)=3s; the hang is stopped inside the container -> FAILED.
    res = _run(fast, GOOD + "import time\ntime.sleep(600)\n")
    assert res.status in (SandboxStatus.FAILED, SandboxStatus.TIMEOUT), res
    ps = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=peh-"],  # noqa: S607
        capture_output=True,
        check=True,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    assert ps.stdout.strip() == ""


def test_daemon_down_simulation_is_sandbox_error() -> None:
    bad = SandboxRunner(image_id="sha256:0", docker="/nonexistent/docker")
    res = _run(bad, GOOD)
    assert res.status is SandboxStatus.SANDBOX_ERROR


def test_no_such_image_is_sandbox_error() -> None:
    res = _run(SandboxRunner(image_id="sha256:" + "0" * 64, timeout_s=60), GOOD)
    assert res.status is SandboxStatus.SANDBOX_ERROR, res


def test_no_leftover_containers() -> None:
    ps = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", "name=peh-"],  # noqa: S607
        capture_output=True,
        check=True,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    assert ps.stdout.strip() == ""
