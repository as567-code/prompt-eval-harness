"""Hardened subprocess runner: the single choke point for launching CLI subprocesses.

Guarantees: argv only (never a shell), stdin closed, allowlisted environment, a hard timeout
that kills the whole process group, and bounded captured output.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

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


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def run_cli(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    max_output_bytes: int = 4_000_000,
    extra_env: Mapping[str, str] | None = None,
) -> CliResult:
    """Run ``argv`` without a shell, with a minimal env, a hard timeout and capped output."""
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
    streams = (proc.stdout, proc.stderr)
    if proc.stdout is None or proc.stderr is None:  # unreachable with PIPE; narrows types
        raise RuntimeError("subprocess pipes were not created")
    bufs = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
    truncated = False
    timed_out = False
    sel = selectors.DefaultSelector()
    try:
        for fd in bufs:
            sel.register(fd, selectors.EVENT_READ)
        open_fds = set(bufs)
        deadline = start + timeout_s
        grace_end: float | None = None
        while open_fds:
            now = time.monotonic()
            if grace_end is None:
                if proc.poll() is not None:
                    _kill_group(proc.pid)  # leftover group members must not outlive the run
                    grace_end = now + POST_EXIT_GRACE_S
                elif now >= deadline:
                    timed_out = True
                    _kill_group(proc.pid)
                    proc.wait()
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
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc.pid)
                proc.wait()
        _kill_group(proc.pid)
    except BaseException:
        _kill_group(proc.pid)
        proc.wait()
        raise
    finally:
        sel.close()
        for stream in streams:
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
