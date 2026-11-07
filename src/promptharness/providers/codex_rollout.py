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
FAIL_CLOSED_OUTPUTS: frozenset[str] = frozenset({"code-mode host is disabled"})
_EXCERPT_MAX = 200
_TEXT_PART_TYPES = ("text", "output_text", "input_text")
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
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if isinstance(p.get("text"), str) and p.get("type") in _TEXT_PART_TYPES:
                    parts.append(p["text"])
                else:
                    parts.append(json.dumps(p, sort_keys=True, default=repr))
            else:
                parts.append(repr(p))
        return "".join(parts)
    return "" if output is None else json.dumps(output)


def audit_rollout(path: Path) -> RolloutAudit:
    cwd: Path | None = None
    model: str | None = None
    effort: str | None = None
    # Ordered items: ("call", type, call_id, name) or ("out", type, call_id, text)
    items: list[tuple[str, str, str, str]] = []
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
            raw_type = payload.get("type")
            if not isinstance(raw_type, str):
                items.append(("call", "<anomalous>", "", "<anomalous>"))
                continue
            ptype = raw_type.lower()
            cid = payload.get("call_id")
            cid = cid if isinstance(cid, str) else ""
            if ptype.endswith("_call"):
                name = payload.get("name")
                items.append(("call", ptype, cid, name if isinstance(name, str) else ptype))
            elif ptype.endswith("_output"):
                items.append(("out", ptype, cid, _output_text(payload.get("output"))))

    call_id_counts: dict[str, int] = {}
    for kind_, _t, cid, _v in items:
        if kind_ == "call" and cid:
            call_id_counts[cid] = call_id_counts.get(cid, 0) + 1
    consumed: set[int] = set()
    tool_calls: list[ToolCall] = []
    for kind_, ptype, cid, name in items:
        if kind_ != "call":
            continue
        out_idx: int | None = None
        if cid:
            for j, (k2, _t2, cid2, _v2) in enumerate(items):
                if k2 == "out" and j not in consumed and cid2 == cid:
                    out_idx = j
                    break
        text = ""
        if out_idx is not None:
            consumed.add(out_idx)
            text = items[out_idx][3]
        known = ptype in _CALL_TYPES
        closed = (
            known
            and out_idx is not None
            and call_id_counts.get(cid, 0) == 1
            and text.strip() in FAIL_CLOSED_OUTPUTS
        )
        tool_calls.append(
            ToolCall(name=name, output_excerpt=text[:_EXCERPT_MAX], failed_closed=closed)
        )
    for j, (k2, _t2, _cid2, text) in enumerate(items):
        if k2 == "out" and j not in consumed:
            tool_calls.append(
                ToolCall(name="<orphan>", output_excerpt=text[:_EXCERPT_MAX], failed_closed=False)
            )
    return RolloutAudit(
        path=path, cwd=cwd, model=model, effort=effort, tool_calls=tuple(tool_calls)
    )


def delete_own_rollout(audit: RolloutAudit, *, own_root: Path) -> bool:
    """Unlink the rollout only if its cwd is inside ``own_root``."""
    if audit.cwd is None or not audit.cwd.is_absolute():
        return False
    if not audit.cwd.resolve().is_relative_to(own_root.resolve()):
        return False
    if not audit.path.exists():
        return False
    audit.path.unlink(missing_ok=True)
    return True
