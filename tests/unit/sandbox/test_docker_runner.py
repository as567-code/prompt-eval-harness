"""Unit tests for SandboxRunner using fake ``docker`` executables (no daemon needed)."""

import importlib.util
import io
import json
import os
import stat
import tarfile
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from promptharness.sandbox.docker_runner import (
    SandboxResult,
    SandboxRunner,
    SandboxStatus,
    build_tar,
)

RUNNER_PATH = Path(__file__).resolve().parents[3] / "sandbox" / "runner.py"

PASSING_TESTS = "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n"
FILES = {"solution.py": "x = 1\n", "test_hidden.py": PASSING_TESTS}
JUNIT_OK = (
    '<testsuites><testsuite><testcase classname="test_hidden" name="test_a"/>'
    '<testcase classname="test_hidden" name="test_b"/></testsuite></testsuites>'
)
JUNIT_FAIL = JUNIT_OK.replace('name="test_b"/>', 'name="test_b"><failure/></testcase>')
JUNIT_EXTRA = JUNIT_OK.replace(
    "</testsuite>", '<testcase classname="test_hidden" name="test_z"/></testsuite>'
)
JUNIT_MISSING = (
    '<testsuites><testsuite><testcase classname="test_hidden" name="test_a"/>'
    "</testsuite></testsuites>"
)
JUNIT_COLLECT = (
    '<testsuites><testsuite><testcase classname="" name="test_hidden"><error/></testcase>'
    "</testsuite></testsuites>"
)


def envelope(
    exit_code: int = 0, junit: str | None = JUNIT_OK, error: str | None = None, **kw: str
) -> dict[str, object]:
    return {
        "v": 1,
        "exit_code": exit_code,
        "junit_xml": junit,
        "stdout_tail": kw.get("stdout_tail", "out"),
        "stderr_tail": kw.get("stderr_tail", "err"),
        "error": error,
    }


def fake_docker(
    tmp_path: Path,
    *,
    stdout: bytes = b"",
    stderr: str = "",
    code: int = 0,
    sleep: float = 0,
) -> Path:
    """A fake docker: logs argv and stdin, then prints canned output."""
    out_file = tmp_path / "stdout.bin"
    out_file.write_bytes(stdout)
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "kill" ]; then printf "%s\\n" "$@" > "{tmp_path}/kill.txt"; exit 0; fi\n'
        f'if [ "$1" = "rm" ]; then printf "%s\\n" "$@" > "{tmp_path}/rm.txt"; exit 0; fi\n'
        f'printf "%s\\n" "$@" > "{tmp_path}/argv.txt"\n'
        f'cat > "{tmp_path}/stdin.tar"\n'
        f"sleep {sleep}\n"
        f'cat "{out_file}"\n'
        f"printf %s '{stderr}' >&2\n"
        f"exit {code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def run_with(tmp_path: Path, files: dict[str, str] | None = None, **fake: object) -> SandboxResult:
    docker = fake_docker(tmp_path, **fake)  # type: ignore[arg-type]
    runner = SandboxRunner(image_id="sha256:abc", docker=str(docker), timeout_s=30)
    return runner.run(files or FILES, test_files=["test_hidden.py"])


def line(env: dict[str, object]) -> bytes:
    return (json.dumps(env) + "\n").encode()


def test_daemon_down_is_sandbox_error(tmp_path: Path) -> None:
    res = run_with(tmp_path, stderr="Cannot connect to the Docker daemon at unix:///x", code=125)
    assert res.status is SandboxStatus.SANDBOX_ERROR
    assert "Cannot connect" in res.detail


def test_missing_docker_binary_is_sandbox_error(tmp_path: Path) -> None:
    runner = SandboxRunner(image_id="sha256:abc", docker=str(tmp_path / "nope"))
    res = runner.run(FILES, test_files=["test_hidden.py"])
    assert res.status is SandboxStatus.SANDBOX_ERROR


@pytest.mark.parametrize("code", [125, 126, 127])
def test_docker_exit_codes_are_sandbox_error(tmp_path: Path, code: int) -> None:
    assert run_with(tmp_path, code=code).status is SandboxStatus.SANDBOX_ERROR


def test_no_such_image_is_sandbox_error(tmp_path: Path) -> None:
    res = run_with(tmp_path, stderr="Unable to find image: No such image: x", code=1)
    assert res.status is SandboxStatus.SANDBOX_ERROR


def test_passed(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope()))
    assert res.status is SandboxStatus.PASSED
    assert (res.passed, res.total) == (2, 2)
    assert res.stdout_tail == "out"
    assert res.stderr_tail == "err"
    assert len(res.outcomes) == 2


def test_failed(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope(1, JUNIT_FAIL)))
    assert res.status is SandboxStatus.FAILED
    assert (res.passed, res.total) == (1, 2)


def test_missing_and_extra_tests_are_tamper(tmp_path: Path) -> None:
    assert (
        run_with(tmp_path, stdout=line(envelope(0, JUNIT_MISSING))).status
        is SandboxStatus.TAMPER_SUSPECT
    )
    assert (
        run_with(tmp_path, stdout=line(envelope(0, JUNIT_EXTRA))).status
        is SandboxStatus.TAMPER_SUSPECT
    )


def test_exit5_with_empty_report_is_tamper(tmp_path: Path) -> None:
    empty = "<testsuites><testsuite/></testsuites>"
    res = run_with(tmp_path, stdout=line(envelope(5, empty)))
    assert res.status is SandboxStatus.TAMPER_SUSPECT


def test_all_pass_report_with_nonzero_exit_is_tamper(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope(1, JUNIT_OK)))
    assert res.status is SandboxStatus.TAMPER_SUSPECT


def test_no_tests_expected_and_none_reported_is_failed(tmp_path: Path) -> None:
    empty = "<testsuites><testsuite/></testsuites>"
    files = {"solution.py": "", "test_hidden.py": "def test_(:\n"}
    res = run_with(tmp_path, files, stdout=line(envelope(5, empty)))
    assert res.status is SandboxStatus.FAILED


def test_collection_error_is_failed(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope(2, JUNIT_COLLECT)))
    assert res.status is SandboxStatus.FAILED
    assert "collection" in res.detail


def test_skipped_is_not_passed(tmp_path: Path) -> None:
    skipped = JUNIT_OK.replace('name="test_b"/>', 'name="test_b"><skipped/></testcase>')
    assert run_with(tmp_path, stdout=line(envelope(0, skipped))).status is SandboxStatus.FAILED


@pytest.mark.parametrize(
    "env",
    [
        envelope(0, None),
        envelope(-1, None, "inner_timeout"),
        envelope(0, None, "report_not_regular"),
        envelope(0, None, "report_too_large"),
        envelope(0, None, "runner_internal: ValueError: boom"),
        envelope(-1, None, "bad_tar: bad member name"),
    ],
)
def test_no_junit_is_failed_never_sandbox_error(tmp_path: Path, env: dict[str, object]) -> None:
    res = run_with(tmp_path, stdout=line(env))
    assert res.status is SandboxStatus.FAILED
    assert res.detail.startswith("no report")
    if env["error"]:
        assert str(env["error"]) in res.detail


def test_malformed_report_is_failed(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope(0, "<not xml")))
    assert res.status is SandboxStatus.FAILED
    assert res.detail == "malformed report"


def test_nodumpable_is_sandbox_error(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope(-1, None, "nodumpable")), code=3)
    assert res.status is SandboxStatus.SANDBOX_ERROR
    assert "nodumpable" in res.detail


def test_exit_137_without_envelope_is_resource_limit(tmp_path: Path) -> None:
    assert run_with(tmp_path, code=137).status is SandboxStatus.RESOURCE_LIMIT


def test_no_envelope_is_sandbox_error_with_stderr(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=b"garbage\n", stderr="weird failure", code=1)
    assert res.status is SandboxStatus.SANDBOX_ERROR
    assert "weird failure" in res.detail


def test_envelope_is_last_valid_line_and_binary_noise_tolerated(tmp_path: Path) -> None:
    forged = envelope(0, JUNIT_OK, stdout_tail="forged")
    real = envelope(1, JUNIT_FAIL, stdout_tail="real")
    out = b"\xff" * 100 + b"\n" + line(forged) + b'{"v": 2}\n' + line(real) + b"trailing junk\n"
    res = run_with(tmp_path, stdout=out)
    assert res.status is SandboxStatus.FAILED
    assert res.stdout_tail == "real"


@pytest.mark.parametrize(
    "bad",
    [
        {
            "v": 1,
            "exit_code": "0",
            "junit_xml": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "error": None,
        },
        {
            "v": 1,
            "exit_code": 0,
            "junit_xml": 5,
            "stdout_tail": "",
            "stderr_tail": "",
            "error": None,
        },
        {"v": 1, "exit_code": 0},
        [1, 2],
    ],
)
def test_ill_typed_envelope_is_not_an_envelope(tmp_path: Path, bad: object) -> None:
    res = run_with(tmp_path, stdout=(json.dumps(bad) + "\n").encode())
    assert res.status is SandboxStatus.SANDBOX_ERROR


def test_host_timeout_kills_container_and_returns_timeout(tmp_path: Path) -> None:
    docker = fake_docker(tmp_path, stdout=line(envelope()), sleep=5)
    runner = SandboxRunner(image_id="sha256:abc", docker=str(docker), timeout_s=0.3, grace_s=0.2)
    res = runner.run(FILES, test_files=["test_hidden.py"])
    assert res.status is SandboxStatus.TIMEOUT
    kill = (tmp_path / "kill.txt").read_text().split()
    assert kill[0] == "kill"
    assert kill[1].startswith("peh-")
    assert (tmp_path / "rm.txt").read_text().split() == ["rm", "-f", kill[1]]
    argv = (tmp_path / "argv.txt").read_text().split()
    assert argv[argv.index("--name") + 1] == kill[1]


def test_docker_argv_is_prd_verbatim(tmp_path: Path) -> None:
    run_with(tmp_path, stdout=line(envelope()))
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    name = argv[argv.index("--name") + 1]
    assert name.startswith("peh-") and len(name) == len("peh-") + 32
    assert argv[:3] == ["run", "--rm", "-i"]
    assert argv[-1] == "sha256:abc"
    expected = [
        "--network", "none", "--read-only",
        "--tmpfs", "/work:rw,size=64m,uid=10001,gid=10001,mode=0700",
        "--tmpfs", "/tmp:rw,size=16m,uid=10001,gid=10001,mode=0700",  # noqa: S108
        "--user", "10001:10001", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
        "--ulimit", "nofile=256:256", "--ulimit", "fsize=33554432",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PEH_INNER_TIMEOUT=25",
        "--name", name,
    ]  # fmt: skip
    assert argv[3:-1] == expected


def test_inner_timeout_floor(tmp_path: Path) -> None:
    docker = fake_docker(tmp_path, stdout=line(envelope()))
    SandboxRunner(image_id="i", docker=str(docker), timeout_s=3).run(
        FILES, test_files=["test_hidden.py"]
    )
    assert "PEH_INNER_TIMEOUT=1" in (tmp_path / "argv.txt").read_text().splitlines()


def test_tar_sent_on_stdin_is_accepted_by_the_runner(tmp_path: Path) -> None:
    run_with(tmp_path, stdout=line(envelope()))
    spec = importlib.util.spec_from_file_location("peh_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    mod: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = mod.safe_extract((tmp_path / "stdin.tar").read_bytes(), tmp_path / "dest")
    assert sorted(names) == ["solution.py", "test_hidden.py"]


def test_build_tar_shape_and_runner_acceptance(tmp_path: Path) -> None:
    files = {"solution.py": "a", "test_hidden.py": "b", "context/x.py": "c", "context/d/y.txt": "é"}
    data = build_tar(files)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = tf.getmembers()
        assert tf.format == tarfile.PAX_FORMAT
        assert [m.name for m in members] == sorted(files)
        assert all(m.isreg() and m.mode == 0o644 for m in members)
        assert tf.extractfile("context/d/y.txt").read() == "é".encode()  # type: ignore[union-attr]
    spec = importlib.util.spec_from_file_location("peh_runner2", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert sorted(mod.safe_extract(data, tmp_path / "d")) == sorted(files)  # never bad_tar


@pytest.mark.parametrize(
    "name",
    ["test_hidden.TestX.py", "a.b/test_x.py",
     "./x.py", "/abs.py", "../x.py", "a/../b.py", "a//b.py", "a b.py", "-x.py", "report.xml",
     "context/report.xml", "", "a/", "é.py", "a" * 300, "/".join(["a"] * 2 + ["b" * 101])],
)  # fmt: skip
def test_build_tar_rejects_bad_names(name: str) -> None:
    with pytest.raises(ValueError):
        build_tar({name: "x"})


def test_build_tar_rejects_collisions_and_size() -> None:
    with pytest.raises(ValueError):
        build_tar({"a": "x", "a/b.py": "x"})
    with pytest.raises(ValueError):
        build_tar({f"f{i}.py": "x" for i in range(65)})
    with pytest.raises(ValueError):
        build_tar({"big.py": "x" * (8 * 1024 * 1024 + 1)})


def test_run_rejects_unknown_test_file(tmp_path: Path) -> None:
    runner = SandboxRunner(image_id="i", docker=str(tmp_path / "nope"))
    with pytest.raises(ValueError):
        runner.run({"solution.py": ""}, test_files=["test_hidden.py"])


def test_concurrency_semaphore_limits_parallel_runs(tmp_path: Path) -> None:
    docker = fake_docker(tmp_path, stdout=line(envelope()), sleep=0.3)
    runner = SandboxRunner(image_id="i", docker=str(docker), concurrency=1)
    t0 = time.monotonic()
    ts = [
        threading.Thread(
            target=runner.run, args=(FILES,), kwargs={"test_files": ["test_hidden.py"]}
        )
        for _ in range(2)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert time.monotonic() - t0 >= 0.6


def test_fake_docker_env_has_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    script = tmp_path / "docker"
    script.write_text(f'#!/bin/sh\nenv > "{tmp_path}/env.txt"\ncat > /dev/null\nexit 125\n')
    script.chmod(0o755)
    SandboxRunner(image_id="i", docker=str(script)).run(FILES, test_files=["test_hidden.py"])
    assert "sk-secret" not in (tmp_path / "env.txt").read_text()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-secret"


def test_valid_envelope_wins_over_infra_stderr(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope()), stderr="No such image: x")
    assert res.status is SandboxStatus.PASSED


def test_valid_failing_envelope_wins_over_docker_exit_code(tmp_path: Path) -> None:
    res = run_with(tmp_path, stdout=line(envelope(1, JUNIT_FAIL)), code=125)
    assert res.status is SandboxStatus.FAILED


def _run_candidate(tmp_path: Path, candidate: str | None, **fake: object) -> SandboxResult:
    docker = fake_docker(tmp_path, **fake)  # type: ignore[arg-type]
    runner = SandboxRunner(image_id="sha256:abc", docker=str(docker), timeout_s=30)
    return runner.run(FILES, test_files=["test_hidden.py"], candidate=candidate)


def test_forgery_candidate_turns_passed_into_tamper(tmp_path: Path) -> None:
    res = _run_candidate(tmp_path, "import os\nos._exit(0)\n", stdout=line(envelope()))
    assert res.status is SandboxStatus.TAMPER_SUSPECT
    assert "_exit" in res.detail
    assert (tmp_path / "argv.txt").exists()  # the container still ran


def test_forgery_candidate_turns_failed_into_tamper(tmp_path: Path) -> None:
    res = _run_candidate(
        tmp_path, "x = 'report.xml'", stdout=line(envelope(junit=None, exit_code=1))
    )
    assert res.status is SandboxStatus.TAMPER_SUSPECT
    assert "report.xml" in res.detail


def test_clean_or_absent_candidate_is_unchanged(tmp_path: Path) -> None:
    assert _run_candidate(tmp_path, "x = 1\n", stdout=line(envelope())).status is (
        SandboxStatus.PASSED
    )
    assert _run_candidate(tmp_path, None, stdout=line(envelope())).status is SandboxStatus.PASSED


def test_forgery_does_not_outrank_timeout_or_sandbox_error(tmp_path: Path) -> None:
    docker = fake_docker(tmp_path, stdout=line(envelope()), sleep=5)
    runner = SandboxRunner(image_id="sha256:abc", docker=str(docker), timeout_s=0.3, grace_s=0.2)
    res = runner.run(FILES, test_files=["test_hidden.py"], candidate="import ctypes")
    assert res.status is SandboxStatus.TIMEOUT
    res2 = _run_candidate(
        tmp_path, "import ctypes", code=125, stderr="Cannot connect to the Docker daemon"
    )
    assert res2.status is SandboxStatus.SANDBOX_ERROR
