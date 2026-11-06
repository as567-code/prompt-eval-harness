import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from promptharness.providers.subprocess_runner import CliResult, run_cli


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_cli.py"
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IRWXU | 0o755)
    return path


def _pgrep(pattern: str) -> list[str]:
    out = subprocess.run(  # noqa: S603 - fixed argv, test helper
        ["pgrep", "-f", pattern],  # noqa: S607 - pgrep from PATH is fine in tests
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.split()


def test_argv_passes_verbatim(tmp_path: Path) -> None:
    script = _script(tmp_path, "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    nasty = "$(rm -rf ~); `id` && echo pwned"
    result = run_cli([str(script), nasty, "b"], cwd=tmp_path, timeout_s=20)
    assert isinstance(result, CliResult)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [nasty, "b"]


def test_stdin_is_closed(tmp_path: Path) -> None:
    script = _script(tmp_path, "import sys\nprint(sys.stdin.read() == '')\n")
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=20)
    assert result.stdout.strip() == "True"


def test_env_is_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    script = _script(tmp_path, "import os\nprint(os.environ.get('ANTHROPIC_API_KEY'))\n")
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=20)
    assert result.stdout.strip() == "None"


def test_extra_env_cannot_inject_base_url(tmp_path: Path) -> None:
    script = _script(tmp_path, "import os\nprint(os.environ.get('ANTHROPIC_BASE_URL'))\n")
    result = run_cli(
        [str(script)],
        cwd=tmp_path,
        timeout_s=20,
        extra_env={"ANTHROPIC_BASE_URL": "http://evil"},
    )
    assert result.stdout.strip() == "None"


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
        import subprocess, time
        subprocess.Popen(["sleep", "6061"])
        time.sleep(60)
        """,
    )
    assert _pgrep("sleep 6061") == []
    start = time.monotonic()
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=1)
    assert time.monotonic() - start < 5
    assert result.timed_out is True
    assert result.returncode is None
    deadline = time.monotonic() + 3
    survivors = _pgrep("sleep 6061")
    while survivors and time.monotonic() < deadline:
        time.sleep(0.1)
        survivors = _pgrep("sleep 6061")
    assert survivors == []


def test_output_cap(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
        import sys
        sys.stdout.write("a" * 6_000_000)
        """,
    )
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=30)
    assert result.truncated is True
    assert result.timed_out is False
    assert len(result.stdout.encode()) <= 4_000_000


def test_small_output_not_truncated(tmp_path: Path) -> None:
    script = _script(tmp_path, "print('hi')\n")
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=20)
    assert result.truncated is False
    assert result.wall_ms >= 0


def test_invalid_utf8_output_is_replaced(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "import sys\nsys.stdout.buffer.write(b'\\xff\\xfeok')\n",
    )
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=20)
    assert "ok" in result.stdout


def test_cwd_is_respected(tmp_path: Path) -> None:
    script = _script(tmp_path, "import os\nprint(os.getcwd())\n")
    work = tmp_path / "work"
    work.mkdir()
    result = run_cli([str(script)], cwd=work, timeout_s=20)
    assert result.stdout.strip() == str(work.resolve())
    assert os.path.isdir(work)


def test_both_streams_heavy_no_deadlock(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
        import sys
        for _ in range(60):
            sys.stdout.write("o" * 100_000)
            sys.stderr.write("e" * 100_000)
        """,
    )
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=30)
    assert result.returncode == 0
    assert result.truncated
    assert len(result.stdout) == 4_000_000
    assert result.stderr


def test_escaped_session_pipe_holder_does_not_hang(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
        import subprocess, sys
        subprocess.Popen(["sleep", "6062"], start_new_session=True,
                         stdout=sys.stdout, stderr=sys.stderr)
        print("ok", flush=True)
        """,
    )
    try:
        start = time.monotonic()
        result = run_cli([str(script)], cwd=tmp_path, timeout_s=10)
        assert time.monotonic() - start < 5
        assert result.returncode == 0
        assert "ok" in result.stdout
        assert result.truncated is True
    finally:
        subprocess.run(["pkill", "-f", "sleep 6062"], check=False)  # noqa: S607


def test_normal_exit_kills_leftover_group_members(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
        import subprocess
        subprocess.Popen(["sleep", "6063"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        """,
    )
    result = run_cli([str(script)], cwd=tmp_path, timeout_s=10)
    assert result.returncode == 0
    deadline = time.monotonic() + 3
    survivors = _pgrep("sleep 6063")
    while survivors and time.monotonic() < deadline:
        time.sleep(0.1)
        survivors = _pgrep("sleep 6063")
    assert survivors == []
