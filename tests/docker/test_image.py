"""Docker tests for the sandbox image (skipped when docker or sandbox/IMAGE_ID is absent)."""

import io
import json
import subprocess
import tarfile
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


def _run(files: dict[str, str], *extra: str, timeout: int = 120) -> dict[str, object]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in files.items():
            raw = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))
    proc = subprocess.run(  # noqa: S603
        ["docker", "run", "--rm", "-i", "--network", "none", *extra, _image()],  # noqa: S607
        input=buf.getvalue(),
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    lines = proc.stdout.decode().splitlines()
    assert len(lines) == 1, (proc.stdout[:500], proc.stderr[:500])
    envelope: dict[str, object] = json.loads(lines[0])
    return envelope


def _image() -> str:
    return IMAGE_ID.read_text(encoding="utf-8").strip()


_PASSING_TEST = "def test_ok():\n    assert True\n"


def _report_swap(body: str) -> dict[str, str]:
    conftest = (
        "import atexit, os\n"
        "def _swap():\n"
        "    p = '/work/report.xml'\n"
        "    if os.path.lexists(p):\n"
        "        os.unlink(p)\n"
        f"{body}\n"
        "atexit.register(_swap)\n"
    )
    return {"conftest.py": conftest, "test_a.py": _PASSING_TEST}


@pytest.mark.parametrize(
    "body",
    [
        "    os.symlink('/dev/zero', p)",
        "    os.symlink('/etc/passwd', p)",
        "    os.mkfifo(p)",
    ],
)
def test_report_not_regular(body: str) -> None:
    env = _run(_report_swap(body))
    assert env["junit_xml"] is None
    assert env["error"] == "report_not_regular"


def test_report_too_large() -> None:
    env = _run(_report_swap("    open(p, 'w').write('x' * 3_000_000)"))
    assert env["junit_xml"] is None
    assert env["error"] == "report_too_large"


def test_bad_tar_collision_and_long_name() -> None:
    env = _run({"a": "x", "a/b.py": "x"})
    assert str(env["error"]).startswith("bad_tar:")
    env = _run({"a" * 300: "x"})
    assert str(env["error"]).startswith("bad_tar:")


def test_print_flood_still_emits_envelope() -> None:
    flood = (
        "import sys\n"
        "def test_flood():\n"
        "    chunk = 'x' * (1024 * 1024)\n"
        "    for _ in range(100):\n"
        "        sys.stdout.write(chunk)\n"
        "        sys.stderr.write(chunk)\n"
        "    sys.stdout.flush()\n"
    )
    env = _run({"test_flood.py": flood}, "--memory", "512m", "-e", "PEH_INNER_TIMEOUT=60")
    assert env["error"] is None
    assert len(str(env["stdout_tail"]).encode()) <= 16384
    assert len(str(env["stderr_tail"]).encode()) <= 16384


def test_inner_timeout_kills_group() -> None:
    hang = "import time\ndef test_hang():\n    time.sleep(600)\n"
    env = _run({"test_hang.py": hang}, "-e", "PEH_INNER_TIMEOUT=2")
    assert env["error"] == "inner_timeout"
    assert env["exit_code"] == -1
