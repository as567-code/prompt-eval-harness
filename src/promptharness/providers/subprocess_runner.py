"""Hardened subprocess runner: the single choke point for launching CLI subprocesses.

Guarantees: argv only (never a shell), stdin closed, allowlisted environment, a hard timeout
that kills the whole process group, and bounded captured output.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from promptharness.security import safe_env

_CHUNK = 65536
_JOIN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI run. ``returncode`` is None only when our timeout killed it."""

    returncode: int | None
    stdout: str
    stderr: str
    wall_ms: int
    timed_out: bool
    truncated: bool


class _Reader(threading.Thread):
    """Drains a pipe in chunks, keeping at most ``cap`` bytes and discarding the rest."""

    def __init__(self, stream: IO[bytes], cap: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = cap
        self._buf = bytearray()
        self.truncated = False

    def run(self) -> None:
        try:
            while chunk := self._stream.read(_CHUNK):
                room = self._cap - len(self._buf)
                if room > 0:
                    self._buf += chunk[:room]
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (OSError, ValueError):
            pass

    @property
    def text(self) -> str:
        return bytes(self._buf).decode("utf-8", errors="replace")


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
    if proc.stdout is None or proc.stderr is None:  # unreachable with PIPE; narrows types
        raise RuntimeError("subprocess pipes were not created")
    out_reader = _Reader(proc.stdout, max_output_bytes)
    err_reader = _Reader(proc.stderr, max_output_bytes)
    out_reader.start()
    err_reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    finally:
        out_reader.join(_JOIN_TIMEOUT_S)
        err_reader.join(_JOIN_TIMEOUT_S)
        proc.stdout.close()
        proc.stderr.close()

    return CliResult(
        returncode=None if timed_out else proc.returncode,
        stdout=out_reader.text,
        stderr=err_reader.text,
        wall_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        truncated=out_reader.truncated or err_reader.truncated,
    )
