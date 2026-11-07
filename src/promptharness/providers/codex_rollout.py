"""Audit Codex session rollouts and clean up only our own.

Codex's ``--json`` stdout hides tool calls; they appear only in the rollout
file. The owner runs their own Codex sessions concurrently, so rollouts are
found strictly by thread id (never by mtime) and deleted only when their
``session_meta.cwd`` lies inside our own work root.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

CODEX_SESSIONS_ROOT = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"

_THREAD_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
_FAIL_CLOSED_MARKER = "code-mode host is disabled"
_EXCERPT_MAX = 200
_CALL_TYPES = ("custom_tool_call", "function_call")


@dataclass(frozen=True)
class ToolCall:
    name: str
    output_excerpt: str
    failed_closed: bool


@dataclass(frozen=True)
class RolloutAudit:
    path: Path
    cwd: Path | None
    model: str | None
    effort: str | None
    tool_calls: tuple[ToolCall, ...]

    @property
    def real_tool_output(self) -> bool:
        return any(not c.failed_closed for c in self.tool_calls)


def find_rollout(thread_id: str, *, sessions_root: Path, around: datetime) -> Path:
    if not _THREAD_ID_RE.fullmatch(thread_id):
        raise ValueError(f"invalid thread id: {thread_id!r}")
    local = around.astimezone() if around.tzinfo is not None else around
    matches: list[Path] = []
    for delta in (-1, 0, 1):
        day = local + timedelta(days=delta)
        d = sessions_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        matches.extend(sorted(d.glob(f"rollout-*{thread_id}*.jsonl")))
    if not matches:
        raise FileNotFoundError(f"no rollout for thread {thread_id} under {sessions_root}")
    if len(matches) > 1:
        raise ValueError(f"{len(matches)} rollouts match thread {thread_id}: {matches}")
    return matches[0]


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for p in output:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return "" if output is None else json.dumps(output)


def audit_rollout(path: Path) -> RolloutAudit:
    cwd: Path | None = None
    model: str | None = None
    effort: str | None = None
    calls: dict[str, str] = {}  # call_id -> name, insertion ordered
    outputs: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = obj.get("type")
        if kind == "session_meta":
            if cwd is None and isinstance(payload.get("cwd"), str):
                cwd = Path(payload["cwd"])
        elif kind == "turn_context":
            if isinstance(payload.get("model"), str):
                model = payload["model"]
            if isinstance(payload.get("effort"), str):
                effort = payload["effort"]
        elif kind == "response_item":
            ptype = payload.get("type")
            call_id = str(payload.get("call_id", ""))
            if ptype in _CALL_TYPES:
                calls[call_id] = str(payload.get("name", ""))
            elif ptype in tuple(f"{t}_output" for t in _CALL_TYPES):
                outputs[call_id] = _output_text(payload.get("output"))
    tool_calls = tuple(
        ToolCall(
            name=name,
            output_excerpt=outputs.get(cid, "")[:_EXCERPT_MAX],
            failed_closed=cid in outputs and _FAIL_CLOSED_MARKER in outputs[cid],
        )
        for cid, name in calls.items()
    )
    return RolloutAudit(path=path, cwd=cwd, model=model, effort=effort, tool_calls=tool_calls)


def delete_own_rollout(audit: RolloutAudit, *, own_root: Path) -> bool:
    """Unlink the rollout only if its cwd is inside ``own_root``."""
    if audit.cwd is None:
        return False
    if not audit.cwd.resolve().is_relative_to(own_root.resolve()):
        return False
    if not audit.path.exists():
        return False
    audit.path.unlink(missing_ok=True)
    return True
