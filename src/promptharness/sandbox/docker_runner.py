"""Host-side sandbox runner: ships a tar to the hardened container and grades the envelope."""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum

from promptharness.sandbox.junit import (
    COLLECTION_PREFIX,
    JunitError,
    TestOutcome,
    parse_junit,
)
from promptharness.sandbox.testids import collect_test_ids
from promptharness.security import safe_env

__all__ = [
    "SandboxResult",
    "SandboxRunner",
    "SandboxStatus",
    "TestOutcome",
    "build_tar",
]

# Mirrors the limits enforced by the trusted in-image runner (sandbox/runner.py).
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 64
MAX_NAME_LEN = 255
MAX_COMPONENT_LEN = 100
_NAME_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_INFRA_STDERR = ("Cannot connect to the Docker daemon", "No such image")
_DOCKER_INFRA_CODES = frozenset({125, 126, 127})
_DETAIL_STDERR_CHARS = 500


class SandboxStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit"
    TAMPER_SUSPECT = "tamper_suspect"
    SANDBOX_ERROR = "sandbox_error"


@dataclass(frozen=True)
class SandboxResult:
    status: SandboxStatus
    outcomes: tuple[TestOutcome, ...]
    passed: int
    total: int
    stdout_tail: str
    stderr_tail: str
    wall_ms: int
    detail: str


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name) or len(name) > MAX_NAME_LEN:
        raise ValueError(f"invalid sandbox file name: {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.startswith("-") or len(p) > MAX_COMPONENT_LEN for p in parts):
        raise ValueError(f"invalid sandbox file name: {name!r}")
    if "report.xml" in parts:
        raise ValueError("report.xml is reserved")
    # Dots in a directory or .py stem would make the junit classname -> file mapping ambiguous.
    if any("." in p for p in parts[:-1]) or (
        parts[-1].endswith(".py") and "." in parts[-1][: -len(".py")]
    ):
        raise ValueError(f"invalid sandbox file name (dot in module path): {name!r}")


def build_tar(files: Mapping[str, str]) -> bytes:
    """Build the in-memory PAX tar: bare relative regular-file members, mode 0644."""
    if len(files) > MAX_MEMBERS:
        raise ValueError(f"too many files: {len(files)} > {MAX_MEMBERS}")
    for name in files:
        _validate_name(name)
    # A file may not also be a directory prefix of another member (the extractor would fail).
    for name in files:
        if any(other.startswith(name + "/") for other in files):
            raise ValueError(f"name collides with a directory: {name!r}")
    encoded = {name: files[name].encode("utf-8") for name in sorted(files)}
    if sum(len(v) for v in encoded.values()) > MAX_TOTAL_BYTES:
        raise ValueError("archive too large")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, raw in encoded.items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.REGTYPE
            info.size = len(raw)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def _parse_envelope(stdout: str) -> dict[str, object] | None:
    """Return the LAST stdout line that is a well-typed ``{"v": 1, ...}`` envelope."""
    for raw in reversed(stdout.splitlines()):
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("v") != 1:
            continue
        exit_code = obj.get("exit_code")
        junit = obj.get("junit_xml")
        error = obj.get("error")
        if (
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and (junit is None or isinstance(junit, str))
            and isinstance(obj.get("stdout_tail"), str)
            and isinstance(obj.get("stderr_tail"), str)
            and (error is None or isinstance(error, str))
        ):
            return obj
    return None


class SandboxRunner:
    """Run candidate code plus hidden tests in a fresh hardened container."""

    def __init__(
        self,
        *,
        image_id: str,
        concurrency: int = 4,
        timeout_s: float = 30.0,
        grace_s: float = 5.0,
        docker: str = "docker",
    ) -> None:
        self._image_id = image_id
        self._timeout_s = timeout_s
        self._grace_s = grace_s
        self._docker = docker
        self._sem = threading.BoundedSemaphore(concurrency)

    def _argv(self, name: str) -> list[str]:
        inner_timeout = max(1, int(self._timeout_s) - 5)
        return [
            self._docker, "run", "--rm", "-i",
            "--network", "none", "--read-only",
            "--tmpfs", "/work:rw,size=64m,uid=10001,gid=10001,mode=0700",
            "--tmpfs", "/tmp:rw,size=16m,uid=10001,gid=10001,mode=0700",  # noqa: S108
            "--user", "10001:10001", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "512m", "--memory-swap", "512m",
            "--cpus", "1",
            "--ulimit", "nofile=256:256", "--ulimit", "fsize=33554432",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", f"PEH_INNER_TIMEOUT={inner_timeout}",
            "--name", name,
            self._image_id,
        ]  # fmt: skip

    def run(self, files: Mapping[str, str], *, test_files: Sequence[str]) -> SandboxResult:
        for tf_name in test_files:
            if tf_name not in files:
                raise ValueError(f"test file {tf_name!r} is not in files")
        expected: set[str] = set()
        for tf_name in test_files:
            expected.update(collect_test_ids(tf_name, files[tf_name]))
        tar_bytes = build_tar(files)
        with self._sem:
            return self._run(tar_bytes, expected)

    def _result(
        self,
        status: SandboxStatus,
        start: float,
        detail: str,
        *,
        outcomes: Sequence[TestOutcome] = (),
        stdout_tail: str = "",
        stderr_tail: str = "",
    ) -> SandboxResult:
        return SandboxResult(
            status=status,
            outcomes=tuple(outcomes),
            passed=sum(1 for o in outcomes if o.outcome == "passed"),
            total=len(outcomes),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            wall_ms=int((time.monotonic() - start) * 1000),
            detail=detail,
        )

    def _kill(self, name: str) -> None:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603
                [self._docker, "kill", name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
                env=safe_env(),
            )

    def _run(self, tar_bytes: bytes, expected: set[str]) -> SandboxResult:
        name = f"peh-{uuid.uuid4().hex}"
        start = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603
                self._argv(name),
                input=tar_bytes,
                capture_output=True,
                timeout=self._timeout_s + self._grace_s,
                check=False,
                env=safe_env(),
            )
        except subprocess.TimeoutExpired:
            self._kill(name)
            return self._result(SandboxStatus.TIMEOUT, start, "host timeout")
        except OSError as exc:
            return self._result(SandboxStatus.SANDBOX_ERROR, start, f"cannot run docker: {exc}")

        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        stderr_short = stderr.strip()[-_DETAIL_STDERR_CHARS:]
        env = _parse_envelope(stdout)

        def err(status: SandboxStatus, detail: str) -> SandboxResult:
            return self._result(status, start, detail, stderr_tail=stderr_short)

        if env is None:
            if proc.returncode in _DOCKER_INFRA_CODES or any(s in stderr for s in _INFRA_STDERR):
                return err(
                    SandboxStatus.SANDBOX_ERROR,
                    f"docker error (exit {proc.returncode}): {stderr_short}",
                )
            if proc.returncode == 137:
                return err(SandboxStatus.RESOURCE_LIMIT, "container killed (exit 137)")
            return err(
                SandboxStatus.SANDBOX_ERROR,
                f"no envelope (exit {proc.returncode}): {stderr_short}",
            )
        if proc.returncode == 3 and env["error"] == "nodumpable":
            return err(SandboxStatus.SANDBOX_ERROR, "image broken: nodumpable")

        stdout_tail = str(env["stdout_tail"])
        stderr_tail = str(env["stderr_tail"])
        exit_code = int(str(env["exit_code"]))
        error = env["error"]
        junit = env["junit_xml"]

        def done(
            status: SandboxStatus, detail: str, outcomes: Sequence[TestOutcome] = ()
        ) -> SandboxResult:
            return self._result(
                status,
                start,
                detail,
                outcomes=outcomes,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
            )

        if junit is None:
            return done(SandboxStatus.FAILED, f"no report: {error}" if error else "no report")
        try:
            outcomes = parse_junit(str(junit))
        except JunitError:
            return done(SandboxStatus.FAILED, "malformed report")
        if any(o.node_base.startswith(COLLECTION_PREFIX) for o in outcomes):
            return done(SandboxStatus.FAILED, "collection error", outcomes)
        reported = {o.node_base for o in outcomes}
        if reported != expected:
            missing = sorted(expected - reported)[:5]
            extra = sorted(reported - expected)[:5]
            return done(
                SandboxStatus.TAMPER_SUSPECT,
                f"manifest mismatch: missing={missing} extra={extra} exit_code={exit_code}",
                outcomes,
            )
        if not outcomes:
            return done(SandboxStatus.FAILED, "no tests", outcomes)
        if all(o.outcome == "passed" for o in outcomes):
            if exit_code != 0:
                return done(
                    SandboxStatus.TAMPER_SUSPECT,
                    f"report all-pass but pytest exit_code={exit_code}",
                    outcomes,
                )
            return done(SandboxStatus.PASSED, "", outcomes)
        return done(SandboxStatus.FAILED, "", outcomes)
