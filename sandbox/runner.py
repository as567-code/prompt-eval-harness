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
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import time
from contextlib import suppress
from pathlib import Path

MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 64
TAIL_BYTES = 16 * 1024
REPORT_MAX_BYTES = 2_000_000
MAX_NAME_LEN = 255
MAX_COMPONENT_LEN = 100
DEFAULT_TIMEOUT = 25
POST_EXIT_GRACE_S = 0.5
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
    if len(name) > MAX_NAME_LEN:
        raise ValueError("bad member name: too long")
    parts = name.split("/")
    if any(len(p) > MAX_COMPONENT_LEN for p in parts):
        raise ValueError("bad member name: component too long")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"bad member path: {name!r}")
    if any(p.startswith("-") for p in parts):
        raise ValueError(f"bad member name (leading dash): {name!r}")
    if "report.xml" in parts:
        raise ValueError("bad member name: report.xml is reserved")
    if any("." in p for p in parts[:-1]) or (
        parts[-1].endswith(".py") and "." in parts[-1][: -len(".py")]
    ):
        raise ValueError(f"bad member name (dot in module path): {name!r}")


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
    """Return at most TAIL_BYTES bytes (UTF-8) of the end of ``data``, on a char boundary."""
    if data is None:
        return ""
    raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
    text = raw[-TAIL_BYTES:].decode("utf-8", "replace")
    while (excess := len(text.encode("utf-8")) - TAIL_BYTES) > 0:
        text = text[(excess + 2) // 3 :]
    return text


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


def _timeout_from_env() -> int:
    try:
        value = int(os.environ.get("PEH_INNER_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _kill_group(child: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(child.pid, signal.SIGKILL)


def _run_child(argv: list[str], timeout: int) -> tuple[int, bytes, bytes, bool]:
    """Run ``argv``; keep only the tail of each stream. Returns (rc, out, err, timed_out)."""
    child = subprocess.Popen(  # noqa: S603
        argv,
        cwd=WORK,
        env=CHILD_ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    bufs: dict[int, bytearray] = {}
    sel = selectors.DefaultSelector()
    if child.stdout is None or child.stderr is None:
        raise RuntimeError("pipes not created")
    for stream in (child.stdout, child.stderr):
        bufs[stream.fileno()] = bytearray()
        sel.register(stream, selectors.EVENT_READ)
    timed_out = False
    exited_at: float | None = None
    while sel.get_map():
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            timed_out = True
            break
        if exited_at is None and child.poll() is not None:
            exited_at = now
        if exited_at is not None and now - exited_at >= POST_EXIT_GRACE_S:
            break
        for key, _ in sel.select(min(remaining, 0.1)):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                sel.unregister(key.fileobj)
                continue
            buf = bufs[key.fd]
            buf += chunk
            if len(buf) > 2 * TAIL_BYTES:
                del buf[:-TAIL_BYTES]
    sel.close()
    if not timed_out:
        try:
            child.wait(timeout=max(deadline - time.monotonic(), 0.01))
        except subprocess.TimeoutExpired:
            timed_out = True
    _kill_group(child)
    child.wait()
    out = bytes(bufs[child.stdout.fileno()])
    err = bytes(bufs[child.stderr.fileno()])
    child.stdout.close()
    child.stderr.close()
    return child.returncode, out, err, timed_out


def _read_report() -> tuple[str | None, str | None]:
    """Read report.xml safely. Returns (junit_xml, error)."""
    try:
        fd = os.open(REPORT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "report_not_regular"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "report_not_regular"
        if st.st_size > REPORT_MAX_BYTES:
            return None, "report_too_large"
        data = b""
        while len(data) <= REPORT_MAX_BYTES:
            chunk = os.read(fd, REPORT_MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > REPORT_MAX_BYTES:
            return None, "report_too_large"
        return data.decode("utf-8", "replace"), None
    except OSError:
        return None, "report_not_regular"
    finally:
        os.close(fd)


def main() -> int:
    if not _set_nondumpable():
        _emit(-1, error="nodumpable")
        return 3
    try:
        return _run()
    except Exception as exc:
        _emit(-1, error=f"runner_internal: {type(exc).__name__}: {exc}"[:500])
        return 0


def _run() -> int:
    data = sys.stdin.buffer.read(MAX_TOTAL_BYTES + 1)
    if len(data) > MAX_TOTAL_BYTES:
        _emit(-1, error="bad_tar: input too large")
        return 0
    try:
        names = safe_extract(data, WORK)
    except (ValueError, OSError) as exc:
        _emit(-1, error=f"bad_tar: {exc}")
        return 0
    tests = [n for n in names if TEST_RE.fullmatch(n.rsplit("/", 1)[-1])]
    try:
        REPORT.unlink(missing_ok=True)
    except OSError:
        _emit(-1, error="bad_tar: report path unusable")
        return 0
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
    rc, out, err, timed_out = _run_child(argv, _timeout_from_env())
    if timed_out:
        _emit(-1, stdout_tail=_tail(out), stderr_tail=_tail(err), error="inner_timeout")
        return 0
    junit, report_error = _read_report()
    _emit(
        rc,
        junit_xml=junit,
        stdout_tail=_tail(out),
        stderr_tail=_tail(err),
        error=report_error,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
