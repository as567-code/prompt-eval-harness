"""Trusted in-image runner for promptharness.

Reads a tar from stdin, safely extracts it into /work, runs pytest as a child process with
piped output, and writes exactly one JSON envelope line to stdout. Only this process ever
writes to the container's stdout. Standard library only.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 64
TAIL_BYTES = 16 * 1024
PR_SET_DUMPABLE = 4
NAME_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
TEST_RE = re.compile(r"^test_.*\.py$")
WORK = Path("/work")
REPORT = WORK / "report.xml"
CHILD_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "HOME": "/tmp",  # noqa: S108
}


def _validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"bad member name: {name!r}")
    if name.startswith("/"):
        raise ValueError(f"bad member path (absolute): {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"bad member path: {name!r}")
    if any(p.startswith("-") for p in parts):
        raise ValueError(f"bad member name (leading dash): {name!r}")
    if name == "report.xml":
        raise ValueError("bad member name: report.xml is reserved")


def safe_extract(tar_bytes: bytes, dest: Path) -> list[str]:
    """Extract regular files only into ``dest``; raise ValueError on any violation."""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:")  # noqa: SIM115
    except tarfile.TarError as exc:
        raise ValueError(f"not a valid tar: {exc}") from exc
    names: list[str] = []
    total = 0
    with tf:
        try:
            members = tf.getmembers()
        except tarfile.TarError as exc:
            raise ValueError(f"not a valid tar: {exc}") from exc
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"too many members: {len(members)} > {MAX_MEMBERS}")
        seen: set[str] = set()
        for member in members:
            if not member.isfile():
                raise ValueError(f"member is not a regular file: {member.name!r}")
            _validate_name(member.name)
            if member.name in seen:
                raise ValueError(f"duplicate member: {member.name!r}")
            seen.add(member.name)
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("archive too large")
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        for member in members:
            target = (root / member.name).resolve()
            if root not in target.parents:
                raise ValueError(f"bad member path: {member.name!r}")
            src = tf.extractfile(member)
            if src is None:
                raise ValueError(f"member is not a regular file: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as out:
                out.write(src.read(member.size))
            os.chmod(target, 0o644)
            names.append(member.name)
    return names


def _tail(data: bytes | str | None) -> str:
    if data is None:
        return ""
    raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
    return raw[-TAIL_BYTES:].decode("utf-8", "replace")


def _emit(
    exit_code: int,
    *,
    junit_xml: str | None = None,
    stdout_tail: str = "",
    stderr_tail: str = "",
    error: str | None = None,
) -> None:
    envelope = {
        "v": 1,
        "exit_code": exit_code,
        "junit_xml": junit_xml,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "error": error,
    }
    sys.stdout.write(json.dumps(envelope) + "\n")
    sys.stdout.flush()


def _set_nondumpable() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def main() -> int:
    if not _set_nondumpable():
        _emit(-1, error="nodumpable")
        return 3
    data = sys.stdin.buffer.read(MAX_TOTAL_BYTES + 1)
    if len(data) > MAX_TOTAL_BYTES:
        _emit(-1, error="bad_tar: input too large")
        return 0
    try:
        names = safe_extract(data, WORK)
    except ValueError as exc:
        _emit(-1, error=f"bad_tar: {exc}")
        return 0
    tests = [n for n in names if TEST_RE.fullmatch(n.rsplit("/", 1)[-1])]
    REPORT.unlink(missing_ok=True)
    timeout = int(os.environ.get("PEH_INNER_TIMEOUT", "25"))
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={REPORT}",
        "--",
        *tests,
    ]
    try:
        child = subprocess.run(  # noqa: S603
            argv,
            cwd=WORK,
            env=CHILD_ENV,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            close_fds=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _emit(
            -1,
            stdout_tail=_tail(exc.stdout),
            stderr_tail=_tail(exc.stderr),
            error="inner_timeout",
        )
        return 0
    junit = REPORT.read_text(encoding="utf-8", errors="replace") if REPORT.exists() else None
    _emit(
        child.returncode,
        junit_xml=junit,
        stdout_tail=_tail(child.stdout),
        stderr_tail=_tail(child.stderr),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
