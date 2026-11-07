"""Unit tests for the in-image runner's ``safe_extract`` (trusted code, loaded by path)."""

import importlib.util
import io
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[3] / "sandbox" / "runner.py"


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("peh_runner", RUNNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _tar(members: list[tuple[str, bytes]], *, extra: list[tarfile.TarInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        for info in extra or []:
            tf.addfile(info)
    return buf.getvalue()


def test_extract_ok(tmp_path: Path) -> None:
    data = _tar([("solution.py", b"x = 1\n"), ("sub/test_a.py", b"def test_a(): pass\n")])
    dest = tmp_path / "work"
    names = runner.safe_extract(data, dest)
    assert sorted(names) == ["solution.py", "sub/test_a.py"]
    assert (dest / "solution.py").read_bytes() == b"x = 1\n"
    assert (dest / "sub" / "test_a.py").stat().st_mode & 0o777 == 0o644


def test_extract_rejects_abs_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path"):
        runner.safe_extract(_tar([("/etc/evil.py", b"x")]), tmp_path / "w")


def test_extract_rejects_dotdot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path"):
        runner.safe_extract(_tar([("a/../../evil.py", b"x")]), tmp_path / "w")
    assert not (tmp_path / "evil.py").exists()


def test_extract_rejects_symlink(tmp_path: Path) -> None:
    link = tarfile.TarInfo("link.py")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(ValueError, match="regular"):
        runner.safe_extract(_tar([], extra=[link]), tmp_path / "w")


def test_extract_rejects_too_many_members(tmp_path: Path) -> None:
    members = [(f"f{i}.py", b"") for i in range(65)]
    with pytest.raises(ValueError, match="members"):
        runner.safe_extract(_tar(members), tmp_path / "w")


def test_extract_rejects_bad_name_chars(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name"):
        runner.safe_extract(_tar([("a b.py", b"x")]), tmp_path / "w")


def test_extract_rejects_oversize(tmp_path: Path) -> None:
    big = b"x" * (5 * 1024 * 1024)
    with pytest.raises(ValueError, match="too large"):
        runner.safe_extract(_tar([("a.py", big), ("b.py", big)]), tmp_path / "w")


def test_extract_rejects_non_tar(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tar"):
        runner.safe_extract(b"not a tar", tmp_path / "w")
