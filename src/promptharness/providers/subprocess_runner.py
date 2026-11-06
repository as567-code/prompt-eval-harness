"""Hardened subprocess runner: the single choke point for launching CLI subprocesses.

Guarantees: argv only (never a shell), stdin closed, allowlisted environment, a hard timeout
that kills the whole process group, and bounded captured output.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from promptharness.security import safe_env

_CHUNK = 65536
POST_EXIT_GRACE_S = 2.0
_POLL_S = 0.05


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI run. ``returncode`` is None only when our timeout killed it.

    ``truncated`` means the output cap was exceeded or the output may be incomplete (a pipe
    did not reach EOF within the post-exit grace period).
    """

    returncode: int | None
    stdout: str
    stderr: str
    wall_ms: int
    timed_out: bool
    truncated: bool


_FLAGS = os.WEXITED | os.WNOWAIT | os.WNOHANG
_EXIT_CODES = frozenset({1, 2, 3})  # CLD_EXITED, CLD_KILLED, CLD_DUMPED (not CLD_STOPPED etc.)
_SIGINFO_SIZE = 128
_SI_CODE_OFFSET = 8  # macOS siginfo_t: si_signo @0, si_errno @4, si_code @8

_libc_waitid: Any = None
if not hasattr(os, "waitid") and sys.platform == "darwin":
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _libc_waitid = _libc.waitid
    _libc_waitid.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    _libc_waitid.restype = ctypes.c_int


def _waitid_nowait(pid: int) -> bool:
    """waitid(P_PID, pid, WEXITED|WNOWAIT|WNOHANG): True once the child has exited (unreaped).

    A stopped child is not exited. Raises ChildProcessError if the child was already reaped.
    os.waitid is missing on macOS, so there libc waitid is called through ctypes and the
    freshly zeroed siginfo buffer is read: the kernel leaves it untouched when nothing is
    waitable, so si_signo != 0 and si_code in CLD_EXITED/KILLED/DUMPED means "exited".
    """
    if hasattr(os, "waitid"):
        res = os.waitid(os.P_PID, pid, _FLAGS)
        return res is not None and res.si_code in _EXIT_CODES
    if _libc_waitid is None:
        raise RuntimeError("waitid unavailable on this platform")
    info = ctypes.create_string_buffer(_SIGINFO_SIZE)  # fresh and zeroed on every call
    if _libc_waitid(os.P_PID, pid, ctypes.addressof(info), _FLAGS) != 0:
        err = ctypes.get_errno()
        if err == errno.ECHILD:
            raise ChildProcessError(err, os.strerror(err))
        raise OSError(err, os.strerror(err))
    signo = int.from_bytes(info.raw[0:4], sys.byteorder, signed=True)
    code = int.from_bytes(
        info.raw[_SI_CODE_OFFSET : _SI_CODE_OFFSET + 4], sys.byteorder, signed=True
    )
    return signo != 0 and code in _EXIT_CODES


def _exited_unreaped(proc: subprocess.Popen[bytes]) -> bool | None:
    """True if the leader exited and is still unreaped; None if it was already reaped."""
    try:
        return _waitid_nowait(proc.pid)
    except ChildProcessError:
        return None


def run_cli(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    max_output_bytes: int = 4_000_000,
    extra_env: Mapping[str, str] | None = None,
) -> CliResult:
    """Run ``argv`` without a shell, with a minimal env, a hard timeout and capped output.

    The process group is killed at most once, and only while the leader is still unreaped
    (a zombie leader pins its pid/pgid, so the signal cannot hit an unrelated process).
    """
    start = time.monotonic()
    # argv is a list and shell is never used; the executable is chosen by trusted callers.
    proc = subprocess.Popen(  # noqa: S603
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=safe_env(extra_env),
        start_new_session=True,
        close_fds=True,
    )
    state = {"killed": False, "reaped": False}

    def kill_group_once() -> None:
        if state["killed"] or state["reaped"]:
            return
        state["killed"] = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    def kill_and_reap() -> None:
        kill_group_once()
        proc.wait()
        state["reaped"] = True

    bufs: dict[int, bytearray] = {}
    out_fd = err_fd = -1
    truncated = False
    timed_out = False
    sel: selectors.BaseSelector | None = None
    try:
        if proc.stdout is None or proc.stderr is None:  # unreachable with PIPE
            raise RuntimeError("subprocess pipes were not created")
        out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
        bufs = {out_fd: bytearray(), err_fd: bytearray()}
        sel = selectors.DefaultSelector()
        for fd in bufs:
            sel.register(fd, selectors.EVENT_READ)
        open_fds = set(bufs)
        deadline = start + timeout_s
        grace_end: float | None = None
        while open_fds:
            now = time.monotonic()
            if grace_end is None:
                exited = _exited_unreaped(proc)
                if exited is None:
                    state["reaped"] = True
                    proc.poll()  # reaped elsewhere: Popen records a returncode on ECHILD
                    grace_end = now + POST_EXIT_GRACE_S
                elif exited:
                    kill_and_reap()  # leftover group members must not outlive the run
                    grace_end = now + POST_EXIT_GRACE_S
                elif now >= deadline:
                    timed_out = True
                    kill_and_reap()
                    grace_end = now + POST_EXIT_GRACE_S
            limit = deadline if grace_end is None else grace_end
            if now >= limit:
                truncated = True  # pipe(s) still open: output may be incomplete
                break
            for key, _ in sel.select(timeout=min(_POLL_S, limit - now)):
                fd = key.fd
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    sel.unregister(fd)
                    open_fds.discard(fd)
                    continue
                buf = bufs[fd]
                room = max_output_bytes - len(buf)
                if room > 0:
                    buf += chunk[:room]
                if len(chunk) > max(room, 0):
                    truncated = True
        if not state["reaped"]:
            exited = _exited_unreaped(proc)
            while exited is False and time.monotonic() < deadline:
                time.sleep(_POLL_S / 5)
                exited = _exited_unreaped(proc)
            if exited is None:
                state["reaped"] = True
                proc.poll()
            else:
                timed_out = timed_out or not exited
                kill_and_reap()
    except BaseException:
        if not state["reaped"]:
            try:
                already_reaped = _exited_unreaped(proc) is None
            except Exception:
                already_reaped = False  # unreaped as far as Popen knows: still kill the group
            if already_reaped:
                state["reaped"] = True
            else:
                kill_and_reap()
        raise
    finally:
        if sel is not None:
            sel.close()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    return CliResult(
        returncode=None if timed_out else proc.returncode,
        stdout=bytes(bufs[out_fd]).decode("utf-8", errors="replace"),
        stderr=bytes(bufs[err_fd]).decode("utf-8", errors="replace"),
        wall_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        truncated=truncated,
    )
