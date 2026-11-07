"""Codex CLI provider (ChatGPT login, ``codex exec --json``, read-only, no tools).

Every call is audited through its session rollout (found by thread id, never by mtime):
the served model and effort must match the request and any real tool output raises
``ToolUseDetected``. Only our own rollouts (cwd under ``work_root``) are ever deleted.
"""

from __future__ import annotations

import contextlib
import functools
import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from promptharness.providers.base import (
    MAX_RAW_BYTES,
    AuthModeError,
    CliError,
    CliTimeout,
    Completion,
    CompletionRequest,
    ServedModelMismatch,
    Status,
    ToolUseDetected,
    UsageLimitError,
)
from promptharness.providers.codex_rollout import (
    CODEX_SESSIONS_ROOT,
    RolloutAudit,
    audit_rollout,
    delete_own_rollout,
    find_rollout,
)
from promptharness.providers.subprocess_runner import CliResult, run_cli
from promptharness.usage import Usage

CODEX_DISABLED_FEATURES: tuple[str, ...] = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "multi_agent",
    "memories",
    "plugins",
    "skill_search",
    "sleep_tool",
    "shell_tool",
    "unified_exec",
    "view_image",
    "goals",
    "tool_suggest",
    "hooks",
    "in_app_browser",
    "realtime_conversation",
    "code_mode_host",
)

_USAGE_LIMIT_RE = re.compile(r"usage limit|hit your usage|rate.?limit|try again in|quota", re.I)
_TRANSIENT_RE = re.compile(r"stream disconnected|overloaded|timeout|5\d\d", re.I)
_IGNORED_ERROR = "Code Mode is unavailable"
_CHATGPT_LOGIN = "Logged in using ChatGPT"
_AUX_TIMEOUT_S = 30.0


def build_codex_argv(
    req: CompletionRequest,
    *,
    binary: str,
    instructions_file: Path,
    schema_file: Path | None,
    cwd: Path,
) -> list[str]:
    """Build the exec argv. No output cap is ever passed (Codex ignores it, R14)."""
    argv = [
        binary,
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-m",
        req.model,
        "-c",
        f'model_reasoning_effort="{req.effort}"',
        "-c",
        f'model_instructions_file="{instructions_file}"',
        "-c",
        'web_search="disabled"',
    ]
    for feature in CODEX_DISABLED_FEATURES:
        argv += ["--disable", feature]
    if schema_file is not None:
        argv += ["--output-schema", str(schema_file)]
    # End of options: the prompt is untrusted text and must never be parsed as a flag.
    argv += ["-C", str(cwd), "--", req.prompt]
    return argv


@dataclass(frozen=True)
class CodexEvents:
    thread_id: str
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    usage: Mapping[str, int] | None = None


def parse_codex_events(stdout: str) -> CodexEvents:
    thread_id = ""
    messages: list[str] = []
    errors: list[str] = []
    usage: Mapping[str, int] | None = None

    def add_error(msg: object) -> None:
        if isinstance(msg, str) and _IGNORED_ERROR not in msg:
            errors.append(msg)

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "thread.started" and isinstance(ev.get("thread_id"), str) and not thread_id:
            thread_id = ev["thread_id"]
        elif kind == "item.completed" and isinstance(ev.get("item"), dict):
            item = ev["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
            elif item.get("type") == "error":
                add_error(item.get("message"))
        elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = {k: v for k, v in ev["usage"].items() if isinstance(v, int)}
        elif kind == "turn.failed" and isinstance(ev.get("error"), dict):
            add_error(ev["error"].get("message"))
        elif kind == "error":
            add_error(ev.get("message"))
    return CodexEvents(thread_id=thread_id, messages=messages, errors=errors, usage=usage)


@functools.cache
def codex_version(binary: str) -> str:
    """``codex --version``, run once per process per binary (failures are not cached)."""
    res = run_cli([binary, "--version"], cwd=Path(tempfile.gettempdir()), timeout_s=_AUX_TIMEOUT_S)
    text = res.stdout.strip()
    if res.timed_out or res.returncode != 0 or not text:
        raise CliError(f"{binary} --version failed", transient=False)
    return text


def _usage(raw: Mapping[str, int]) -> Usage:
    cached = raw.get("cached_input_tokens", 0)
    return Usage(
        input=max(raw.get("input_tokens", 0) - cached, 0),
        output=raw.get("output_tokens", 0),
        cache_read=cached,
        cache_write_5m=raw.get("cache_write_input_tokens", 0),
        reasoning=raw.get("reasoning_output_tokens", 0),
    )


def _rollout_quota(path: Path) -> Mapping[str, object] | None:
    """``rate_limits`` of the LAST event_msg/token_count payload, or None."""
    quota: Mapping[str, object] | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if '"token_count"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if (
            isinstance(payload, dict)
            and obj.get("type") == "event_msg"
            and payload.get("type") == "token_count"
        ):
            limits = payload.get("rate_limits")
            quota = limits if isinstance(limits, dict) else None
    return quota


class CodexCLIProvider:
    name = "codex-cli"

    def __init__(
        self,
        *,
        work_root: Path,
        binary: str = "codex",
        sessions_root: Path = CODEX_SESSIONS_ROOT,
    ) -> None:
        self._work_root = work_root
        self._binary = binary
        self._sessions_root = sessions_root
        self._auth_checked = False

    def _check_auth(self) -> None:
        if self._auth_checked:
            return
        res = run_cli(
            [self._binary, "login", "status"],
            cwd=Path(tempfile.gettempdir()),
            timeout_s=_AUX_TIMEOUT_S,
        )
        if _CHATGPT_LOGIN not in res.stdout + res.stderr:
            raise AuthModeError(f"codex login status does not show ChatGPT login: {res.stdout!r}")
        self._auth_checked = True

    def complete(self, req: CompletionRequest) -> Completion:
        req.validate()
        self._check_auth()
        version = codex_version(self._binary)
        started = datetime.now()
        self._work_root.mkdir(parents=True, exist_ok=True)
        workdir = self._work_root / uuid.uuid4().hex
        workdir.mkdir(mode=0o700)
        workdir.chmod(0o700)
        audit: RolloutAudit | None = None
        thread_id = ""
        try:
            instr = workdir / "instr.md"
            instr.write_text(req.system, encoding="utf-8")
            schema: Path | None = None
            if req.json_schema is not None:
                schema = workdir / "schema.json"
                schema.write_text(json.dumps(req.json_schema, sort_keys=True), encoding="utf-8")
            res = run_cli(
                build_codex_argv(
                    req,
                    binary=self._binary,
                    instructions_file=instr,
                    schema_file=schema,
                    cwd=workdir,
                ),
                cwd=workdir,
                timeout_s=req.timeout_s,
            )
            if res.timed_out:
                raise CliTimeout(f"codex timed out after {req.timeout_s}s")
            events = parse_codex_events(res.stdout)
            thread_id = events.thread_id
            if thread_id:
                audit = self._load_audit(thread_id, started)
            return self._map(req, res, events, audit, version)
        finally:
            try:
                if audit is not None:
                    self._cleanup(audit, thread_id)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    def _load_audit(self, thread_id: str, started: datetime) -> RolloutAudit | None:
        try:
            path = find_rollout(thread_id, sessions_root=self._sessions_root, around=started)
            return audit_rollout(path)
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _owned(self, audit: RolloutAudit) -> bool:
        cwd = audit.cwd
        if cwd is None or not cwd.is_absolute():
            return False
        return cwd.resolve().is_relative_to(self._work_root.resolve())

    def _cleanup(self, audit: RolloutAudit, thread_id: str) -> None:
        """Delete our own rollout (``codex delete --force``, else unlink); never anything else."""
        if not self._owned(audit):
            return
        with contextlib.suppress(Exception):  # cleanup must never mask the call result
            res = run_cli(
                [self._binary, "delete", "--force", thread_id],
                cwd=Path(tempfile.gettempdir()),
                timeout_s=_AUX_TIMEOUT_S,
            )
            if res.returncode == 0 and not res.timed_out and not audit.path.exists():
                return
        with contextlib.suppress(OSError):
            delete_own_rollout(audit, own_root=self._work_root)

    def _map(
        self,
        req: CompletionRequest,
        res: CliResult,
        events: CodexEvents,
        audit: RolloutAudit | None,
        version: str,
    ) -> Completion:
        for msg in events.errors:
            if _USAGE_LIMIT_RE.search(msg):
                raise UsageLimitError(msg)
        if not events.messages:
            if events.errors:
                msg = "; ".join(events.errors)
                raise CliError(msg, transient=bool(_TRANSIENT_RE.search(msg)))
            why = "output truncated" if res.truncated else f"exit {res.returncode}"
            raise CliError(f"no agent message in codex output ({why})", transient=False)
        if res.returncode != 0:
            raise CliError(f"codex exited {res.returncode} with an agent message", transient=False)
        if events.usage is None:
            raise CliError("no turn.completed usage in codex output", transient=False)
        if audit is None:
            raise CliError("no rollout found for codex thread; cannot audit tools", transient=False)
        if audit.real_tool_output:
            raise ToolUseDetected(
                "codex tool output: "
                + "; ".join(c.output_excerpt for c in audit.tool_calls if not c.failed_closed)
            )
        if audit.model != req.model:
            raise ServedModelMismatch(req.model, audit.model or "<none>")
        if audit.effort != req.effort:
            raise CliError(
                f"codex ran with effort {audit.effort!r}, requested {req.effort!r}",
                transient=False,
            )
        text = events.messages[-1]
        structured: Mapping[str, object] | None = None
        if req.json_schema is not None:
            try:
                parsed: Any = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CliError(
                    f"schema call returned non-JSON text: {exc}", transient=False
                ) from exc
            if not isinstance(parsed, dict):
                raise CliError("schema call returned non-object JSON", transient=False)
            structured = parsed
        return Completion(
            text=text,
            structured=structured,
            status=Status.OK,
            stop_reason=None,
            served_model=audit.model,
            usage_gross=_usage(events.usage),
            latency_ms_api=None,
            latency_ms_wall=res.wall_ms,
            cli_attempts=1,
            tool_attempts=len(audit.tool_calls),
            output_cap_applied=False,
            cli_version=version,
            provider=self.name,
            raw=res.stdout[:MAX_RAW_BYTES],
            quota=_rollout_quota(audit.path),
        )
