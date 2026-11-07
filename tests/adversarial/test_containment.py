"""Adversarial containment: hostile candidate code must stay inside the sandbox."""

import time
from collections.abc import Callable

import pytest

from promptharness.sandbox.docker_runner import SandboxResult, SandboxRunner, SandboxStatus

pytestmark = [pytest.mark.docker, pytest.mark.adversarial]

TRIVIAL = "from solution import f\n\ndef test_it():\n    assert f() == 1\n"
GOOD_F = "def f():\n    return 1\n"


def _run(runner: SandboxRunner, solution: str, tests: str = TRIVIAL) -> SandboxResult:
    return runner.run(
        {"solution.py": solution, "test_hidden.py": tests}, test_files=["test_hidden.py"]
    )


def _tails(res: SandboxResult) -> str:
    return res.stdout_tail + res.stderr_tail + res.detail


def test_network_blocked(runner: SandboxRunner) -> None:
    solution = (
        "import socket, urllib.request\n"
        "socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "urllib.request.urlopen('http://example.com', timeout=2)\n"
        "def f():\n    return 1\n"
    )
    res = _run(runner, solution)
    assert res.status is not SandboxStatus.PASSED, res
    text = _tails(res).lower()
    assert any(
        s in text for s in ("unreachable", "name resolution", "temporary failure", "errno 101")
    ), text


def test_no_host_env(runner: SandboxRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEH_CANARY_SECRET", "leak123")
    solution = "import os\ndef f():\n    assert False, repr(dict(os.environ))\n"
    res = _run(runner, solution)
    assert res.status is SandboxStatus.FAILED, res
    assert "'PATH'" in _tails(res)  # the child env really was dumped into the assertion message
    assert "leak123" not in _tails(res)
    assert "PEH_CANARY_SECRET" not in _tails(res)


def test_readonly_rootfs(runner: SandboxRunner) -> None:
    tests = (
        "import pytest\n\n"
        "@pytest.mark.parametrize('p', ['/etc/x', '/usr/x'])\n"
        "def test_ro(p):\n"
        "    with pytest.raises(OSError):\n"
        "        open(p, 'w')\n"
    )
    res = _run(runner, GOOD_F, tests)
    assert res.status is SandboxStatus.PASSED, res


def test_work_size_cap(runner: SandboxRunner) -> None:
    solution = (
        "with open('/work/big.bin', 'wb') as fh:\n"
        "    for _ in range(80):\n"
        "        fh.write(b'x' * 1024 * 1024)\n"
        "def f():\n    return 1\n"
    )
    res = _run(runner, solution)
    assert res.status is not SandboxStatus.PASSED, res


def test_fork_bomb(runner: SandboxRunner, leaked_containers: Callable[[], str | None]) -> None:
    solution = "import os\nwhile True:\n    os.fork()\n"
    start = time.monotonic()
    res = _run(runner, solution)
    elapsed = time.monotonic() - start
    assert res.status is not SandboxStatus.PASSED, res
    # Host timeout (30 + 5 grace) plus the kill and rm caps (15 s each); the daemon is starved
    # by the fork bomb, so both can run long.
    assert elapsed < 30 + 5 + 15 + 15 + 5
    assert not leaked_containers()


def test_memory_bomb(runner: SandboxRunner) -> None:
    solution = "x = b'x' * 2_000_000_000\ndef f():\n    return 1\n"
    res = _run(runner, solution)
    # Child OOM with a surviving runner: no report (FAILED). Container kill: RESOURCE_LIMIT.
    assert res.status in (SandboxStatus.RESOURCE_LIMIT, SandboxStatus.FAILED), res


def test_infinite_loop(runner: SandboxRunner) -> None:
    solution = "while True:\n    pass\n"
    start = time.monotonic()
    res = _run(runner, solution)
    assert time.monotonic() - start < 40
    # The inner timeout normally fires first (FAILED, no report); the host timeout is the backstop.
    assert res.status in (SandboxStatus.TIMEOUT, SandboxStatus.FAILED), res


def test_stdout_flood(runner: SandboxRunner) -> None:
    solution = (
        "import sys\n"
        "chunk = 'x' * 1024 * 1024 + '\\n'\n"
        "for _ in range(50):\n"
        "    sys.stdout.write(chunk)\n"
        "def f():\n    return 1\n"
    )
    res = _run(runner, solution)
    assert res.status is not SandboxStatus.SANDBOX_ERROR, res
    assert len(res.stdout_tail.encode()) <= 16 * 1024
    assert len(res.stderr_tail.encode()) <= 16 * 1024


def test_unprivileged(runner: SandboxRunner) -> None:
    tests = (
        "import os\nfrom pathlib import Path\n\n"
        "def test_unpriv():\n"
        "    assert os.getuid() != 0\n"
        "    assert 'CapEff:\\t0000000000000000' in Path('/proc/self/status').read_text()\n"
        "    assert not os.path.exists('/var/run/docker.sock')\n"
    )
    res = _run(runner, GOOD_F, tests)
    assert res.status is SandboxStatus.PASSED, res
