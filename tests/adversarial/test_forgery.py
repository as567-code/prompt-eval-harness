"""Adversarial grader forgery: candidates that try to fake a pass must not be graded PASSED."""

import json

import pytest

from promptharness.sandbox.docker_runner import SandboxResult, SandboxRunner, SandboxStatus

pytestmark = [pytest.mark.docker, pytest.mark.adversarial]

TESTS = "from solution import f\n\ndef test_it():\n    assert f() == 1\n"
FAKE_JUNIT = (
    '<testsuites><testsuite><testcase classname="test_hidden" name="test_it"/>'
    "</testsuite></testsuites>"
)


def _run(runner: SandboxRunner, solution: str, tests: str = TESTS) -> SandboxResult:
    return runner.run(
        {"solution.py": solution, "test_hidden.py": tests},
        test_files=["test_hidden.py"],
    )


def test_os_exit_forgery(runner: SandboxRunner) -> None:
    res = _run(runner, "import os\nos._exit(0)\n")
    assert res.status is SandboxStatus.TAMPER_SUSPECT, res
    assert "_exit" in res.detail


def test_fake_report_forgery(runner: SandboxRunner) -> None:
    solution = f"import os\nopen('/work/report.xml', 'w').write({FAKE_JUNIT!r})\nos._exit(0)\n"
    res = _run(runner, solution)
    assert res.status is SandboxStatus.TAMPER_SUSPECT, res


def test_forged_report_with_os_exit_is_tamper(runner: SandboxRunner) -> None:
    """The ledger residual: a valid all-pass report plus os._exit(0) would otherwise be PASSED."""
    solution = (
        "import os\n"
        f"with open('/work/report.xml', 'w') as fh:\n    fh.write({FAKE_JUNIT!r})\n"
        "os._exit(0)\n"
    )
    res = _run(runner, solution)
    assert res.status is SandboxStatus.TAMPER_SUSPECT, res
    assert "_exit" in res.detail
    assert "report.xml" in res.detail
    # Without the scan the same container outcome is a clean PASSED: this is what the scan closes.
    unscanned = runner.run(
        {"solution.py": solution, "test_hidden.py": TESTS},
        test_files=["test_hidden.py"],
        scan_candidate=False,
    )
    assert unscanned.status is SandboxStatus.PASSED, unscanned


def _envelope_printing_solution(*, obfuscate_key: bool) -> str:
    forged = {
        "v": 1,
        "exit_code": 0,
        "junit_xml": FAKE_JUNIT,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": None,
    }
    if not obfuscate_key:
        return f"print({json.dumps(forged)!r})\ndef f():\n    return 2\n"
    # Build the "junit_xml" key at runtime so the static scan has nothing to match.
    body = {k: v for k, v in forged.items() if k != "junit_xml"}
    return (
        f"import json\nd = {body!r}\n"
        f"d['ju' + 'nit_xml'] = {FAKE_JUNIT!r}\n"
        "print(json.dumps(d))\n"
        "def f():\n    return 2\n"
    )


def test_forged_report_with_execv_is_tamper(runner: SandboxRunner) -> None:
    """Process replacement exits 0 before pytest writes its report; the scan closes that hole."""
    solution = (
        "import os\n"
        f"with open('/work/report.xml', 'w') as fh:\n    fh.write({FAKE_JUNIT!r})\n"
        "os.execv('/bin/true', ['true'])\n"
    )
    res = _run(runner, solution)
    assert res.status is SandboxStatus.TAMPER_SUSPECT, res
    assert "execv" in res.detail
    unscanned = runner.run(
        {"solution.py": solution, "test_hidden.py": TESTS},
        test_files=["test_hidden.py"],
        scan_candidate=False,
    )
    assert unscanned.status is SandboxStatus.PASSED, unscanned


def test_fake_envelope_on_stdout(runner: SandboxRunner) -> None:
    """Scan-evading forged envelope: the runner's own envelope wins, so the real failure stands."""
    res = _run(runner, _envelope_printing_solution(obfuscate_key=True))
    assert res.status is SandboxStatus.FAILED, res


def test_fake_envelope_literal_is_flagged(runner: SandboxRunner) -> None:
    res = _run(runner, _envelope_printing_solution(obfuscate_key=False))
    assert res.status is SandboxStatus.TAMPER_SUSPECT, res


def test_obfuscated_exit_residual(runner: SandboxRunner) -> None:
    """Documents that the static scan is bypassable; the missing report still fails the run."""
    solution = 'getattr(__import__("o" + "s"), "_e" + "xit")(0)\n'
    res = _run(runner, solution)
    assert res.status is not SandboxStatus.PASSED, res
