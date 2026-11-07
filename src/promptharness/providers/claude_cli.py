"""Claude Code CLI provider (subscription OAuth, stream-json, no tools).

Every call is audited: init must report ``apiKeySource == "none"`` and no tools, the served
model must be exactly the requested one, and the turn count / permission denials must show
that no tool ran. Usage comes straight from the ``result`` event.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
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
from promptharness.providers.subprocess_runner import CliResult, run_cli
from promptharness.usage import Usage

_USAGE_LIMIT_RE = re.compile(
    r"usage limit|hit your limit|limit reached|resets? at|rate.?limit", re.I
)
_TRANSIENT_RE = re.compile(r"overloaded|timeout|connection", re.I)
_CAP_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
_VERSION_TIMEOUT_S = 30.0


def build_claude_argv(req: CompletionRequest, *, binary: str = "claude") -> list[str]:
    argv = [
        binary,
        "-p",
        req.prompt,
        "--model",
        req.model,
        "--effort",
        req.effort,
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--system-prompt",
        req.system,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]
    if req.json_schema is not None:
        argv += ["--json-schema", json.dumps(req.json_schema, sort_keys=True)]
    return argv


@dataclass(frozen=True)
class ClaudeStream:
    init: dict[str, Any] | None
    result: dict[str, Any] | None
    rate_limit: dict[str, Any] | None


def _events(stdout: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue  # preamble or noise
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def parse_claude_stream(stdout: str) -> ClaudeStream:
    """Pick the first init, the last result and the last rate_limit_info from stream-json."""
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    rate: dict[str, Any] | None = None
    for ev in _events(stdout):
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init" and init is None:
            init = ev
        elif kind == "result":
            result = ev
        elif kind == "rate_limit_event" and isinstance(ev.get("rate_limit_info"), dict):
            rate = ev["rate_limit_info"]
    return ClaudeStream(init=init, result=result, rate_limit=rate)


def parse_claude_output(stdout: str) -> dict[str, object]:
    """Return the last ``result`` object in stdout; CliError(transient=False) if none."""
    result = parse_claude_stream(stdout).result
    if result is None:
        raise CliError("no result object in claude output", transient=False)
    return result


@functools.cache
def claude_version(binary: str) -> str:
    """``claude --version``, run once per process per binary (failures are not cached)."""
    res = run_cli(
        [binary, "--version"], cwd=Path(tempfile.gettempdir()), timeout_s=_VERSION_TIMEOUT_S
    )
    text = res.stdout.strip()
    if res.timed_out or res.returncode != 0 or not text:
        raise CliError(f"{binary} --version failed", transient=False)
    return text


def _int(obj: Any, key: str) -> int:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(result: Mapping[str, Any]) -> Usage:
    u = result.get("usage")
    u = u if isinstance(u, dict) else {}
    cc = u.get("cache_creation")
    details = u.get("output_tokens_details")
    return Usage(
        input=_int(u, "input_tokens"),
        output=_int(u, "output_tokens"),
        cache_read=_int(u, "cache_read_input_tokens"),
        cache_write_5m=_int(cc, "ephemeral_5m_input_tokens"),
        cache_write_1h=_int(cc, "ephemeral_1h_input_tokens"),
        reasoning=_int(details, "thinking_tokens"),
    )


def _served_model(result: Mapping[str, Any], requested: str) -> str:
    mu = result.get("modelUsage")
    keys = sorted(mu) if isinstance(mu, dict) else []
    if not keys:
        raise ServedModelMismatch(requested, "<none>")
    if len(keys) > 1:
        raise ServedModelMismatch(requested, ",".join(keys))
    if keys[0] != requested:
        raise ServedModelMismatch(requested, keys[0])
    return str(keys[0])


def _check_init(init: Mapping[str, Any]) -> None:
    source = init.get("apiKeySource")
    if source != "none":
        raise AuthModeError(f"claude apiKeySource is {source!r}, expected 'none' (subscription)")
    tools = init.get("tools")
    if tools != []:
        raise ToolUseDetected(f"claude init reported tools: {tools!r}")


def _reset_hint(rate: Mapping[str, Any] | None) -> str | None:
    if rate is not None and rate.get("resetsAt") is not None:
        return str(rate["resetsAt"])
    return None


def _raise_for_error(result: Mapping[str, Any], rate: Mapping[str, Any] | None) -> None:
    text = str(result.get("result") or "")
    if rate is not None and rate.get("status") != "allowed":
        raise UsageLimitError(text or "rate limit not allowed", reset_hint=_reset_hint(rate))
    if _USAGE_LIMIT_RE.search(text):
        raise UsageLimitError(text, reset_hint=_reset_hint(rate))
    status = result.get("api_error_status")
    transient = (isinstance(status, int) and (status >= 500 or status == 429)) or bool(
        _TRANSIENT_RE.search(text)
    )
    raise CliError(text or "claude reported an error", transient=transient)


def _audit_tools(result: Mapping[str, Any], *, schema: bool) -> Mapping[str, Any] | None:
    """Return the structured payload (schema calls) or raise if any tool use is evident."""
    if result.get("permission_denials"):
        raise ToolUseDetected(f"permission_denials: {result.get('permission_denials')!r}")
    turns = result.get("num_turns")
    payload = result.get("structured_output")
    has_payload = isinstance(payload, dict)
    allowed = 2 if (schema and has_payload) else 1
    if not isinstance(turns, int) or turns > allowed or turns < 1:
        raise ToolUseDetected(f"num_turns={turns!r}, allowed <= {allowed}")
    if schema:
        if not has_payload:
            raise CliError("json_schema requested but no structured_output", transient=False)
        return payload
    return None


class ClaudeCLIProvider:
    name = "claude-cli"

    def __init__(self, *, work_root: Path, binary: str = "claude") -> None:
        self._work_root = work_root
        self._binary = binary

    def complete(self, req: CompletionRequest) -> Completion:
        req.validate()
        version = claude_version(self._binary)
        self._work_root.mkdir(parents=True, exist_ok=True)
        workdir = self._work_root / uuid.uuid4().hex
        workdir.mkdir(mode=0o700)
        workdir.chmod(0o700)
        extra_env = (
            {_CAP_ENV: str(req.max_output_tokens)} if req.max_output_tokens is not None else None
        )
        try:
            res = run_cli(
                build_claude_argv(req, binary=self._binary),
                cwd=workdir,
                timeout_s=req.timeout_s,
                extra_env=extra_env,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return self._map(req, res, version)

    def _map(self, req: CompletionRequest, res: CliResult, version: str) -> Completion:
        if res.timed_out:
            raise CliTimeout(f"claude timed out after {req.timeout_s}s")
        stream = parse_claude_stream(res.stdout)
        result = stream.result
        if result is None:
            why = "output truncated" if res.truncated else f"exit {res.returncode}"
            raise CliError(f"no result event in claude output ({why})", transient=False)
        is_error = result.get("is_error") is True
        if stream.init is not None:
            _check_init(stream.init)
        elif not is_error:
            raise CliError("no init event; cannot verify subscription auth", transient=False)

        cap_hit = is_error and result.get("api_error") == "max_output_tokens"
        if is_error and not cap_hit:
            _raise_for_error(result, stream.rate_limit)
        if not is_error and res.returncode != 0:
            raise CliError(f"claude exited {res.returncode} with a success result", transient=False)

        served = _served_model(result, req.model)
        usage = _usage(result)
        api_ms = result.get("duration_api_ms")
        common: dict[str, Any] = {
            "served_model": served,
            "usage_gross": usage,
            "latency_ms_api": api_ms if isinstance(api_ms, int) else None,
            "latency_ms_wall": res.wall_ms,
            "cli_attempts": 1,
            "tool_attempts": 0,
            "cli_version": version,
            "provider": self.name,
            "raw": res.stdout[:MAX_RAW_BYTES],
            "quota": stream.rate_limit,
        }
        if cap_hit:
            if result.get("permission_denials"):
                raise ToolUseDetected(f"permission_denials: {result.get('permission_denials')!r}")
            return Completion(
                text="",
                structured=None,
                status=Status.TRUNCATED,
                stop_reason=_str_or_none(result.get("stop_reason")),
                output_cap_applied=True,
                **common,
            )
        structured = _audit_tools(result, schema=req.json_schema is not None)
        stop = _str_or_none(result.get("stop_reason"))
        status = {"max_tokens": Status.TRUNCATED, "refusal": Status.REFUSAL}.get(
            stop or "", Status.OK
        )
        return Completion(
            text=str(result.get("result") or ""),
            structured=structured,
            status=status,
            stop_reason=stop,
            output_cap_applied=req.max_output_tokens is not None,
            **common,
        )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
