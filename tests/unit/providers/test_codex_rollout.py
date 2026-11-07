from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from promptharness.providers.codex_rollout import (
    audit_rollout,
    delete_own_rollout,
    find_rollout,
)

FIX = Path(__file__).parents[2] / "fixtures" / "cli" / "codex" / "rollouts"
TID = "01aaaaaa-0000-7000-8000-000000000003"


def _line(type_: str, payload: dict[str, Any]) -> str:
    return json.dumps({"timestamp": "t", "type": type_, "payload": payload})


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def _place(root: Path, when: datetime, thread_id: str = TID) -> Path:
    d = root / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"
    d.mkdir(parents=True)
    dest = d / f"rollout-{when:%Y-%m-%dT%H-%M-%S}-{thread_id}.jsonl"
    shutil.copy(FIX / "toolbait_terra.jsonl", dest)
    return dest


def _synthetic(path: Path, cwd: str, output: Any) -> Path:
    return _write(
        path,
        [
            _line("session_meta", {"cwd": cwd}),
            _line("turn_context", {"model": "m1", "effort": "low"}),
            _line("turn_context", {"model": "m2", "effort": "high"}),
            _line("response_item", {"type": "function_call", "call_id": "c1", "name": "shell"}),
            _line(
                "response_item", {"type": "function_call_output", "call_id": "c1", "output": output}
            ),
        ],
    )


def test_audit_toolbait() -> None:
    audit = audit_rollout(FIX / "toolbait_terra.jsonl")
    assert audit.model == "gpt-6.1-terra"
    assert audit.effort == "low"
    assert audit.cwd == Path("/TMP/cwd")
    assert len(audit.tool_calls) == 1
    assert audit.tool_calls[0].failed_closed is True
    assert audit.real_tool_output is False


def test_audit_ok_has_no_tool_calls() -> None:
    audit = audit_rollout(FIX / "ok_terra.jsonl")
    assert audit.tool_calls == ()
    assert audit.real_tool_output is False


def test_audit_flags_real_output(tmp_path: Path) -> None:
    p = _synthetic(tmp_path / "r.jsonl", "/x", "Script completed ... /etc/hostname contents")
    audit = audit_rollout(p)
    assert audit.real_tool_output is True
    assert audit.model == "m2" and audit.effort == "high"


MARKER = "code-mode host is disabled"


def test_audit_excerpt_cap(tmp_path: Path) -> None:
    audit = audit_rollout(_synthetic(tmp_path / "r.jsonl", "/x", "z" * 500))
    assert len(audit.tool_calls[0].output_excerpt) <= 200


def test_marker_plus_real_data_not_closed(tmp_path: Path) -> None:
    s = audit_rollout(_synthetic(tmp_path / "a.jsonl", "/x", MARKER + "\nroot:x:0:0"))
    assert s.real_tool_output is True
    parts = [{"type": "text", "text": MARKER}, {"type": "text", "text": "root:x:0:0"}]
    assert audit_rollout(_synthetic(tmp_path / "b.jsonl", "/x", parts)).real_tool_output is True


def test_pure_marker_closed_string_and_parts(tmp_path: Path) -> None:
    assert (
        audit_rollout(_synthetic(tmp_path / "a.jsonl", "/x", MARKER + "\n")).real_tool_output
        is False
    )
    parts = [{"type": "text", "text": "code-mode host "}, {"type": "text", "text": "is disabled"}]
    assert audit_rollout(_synthetic(tmp_path / "b.jsonl", "/x", parts)).real_tool_output is False


def _items(path: Path, *payloads: dict[str, Any]) -> Path:
    return _write(path, [_line("response_item", p) for p in payloads])


def test_unknown_call_type_with_real_output(tmp_path: Path) -> None:
    p = _items(
        tmp_path / "r.jsonl",
        {"type": "local_shell_call", "call_id": "c1"},
        {"type": "local_shell_call_output", "call_id": "c1", "output": "hi"},
    )
    assert audit_rollout(p).real_tool_output is True


def test_unknown_call_type_never_closed_even_with_marker(tmp_path: Path) -> None:
    p = _items(
        tmp_path / "r.jsonl",
        {"type": "local_shell_call", "call_id": "c1"},
        {"type": "local_shell_call_output", "call_id": "c1", "output": MARKER},
    )
    assert audit_rollout(p).real_tool_output is True


def test_call_without_output_at_all(tmp_path: Path) -> None:
    p = _items(tmp_path / "r.jsonl", {"type": "mcp_tool_call", "call_id": "c1", "name": "x"})
    audit = audit_rollout(p)
    assert len(audit.tool_calls) == 1 and audit.real_tool_output is True


def test_duplicate_call_id_not_closed(tmp_path: Path) -> None:
    p = _items(
        tmp_path / "r.jsonl",
        {"type": "function_call", "call_id": "c1", "name": "a"},
        {"type": "function_call", "call_id": "c1", "name": "b"},
        {"type": "function_call_output", "call_id": "c1", "output": MARKER},
        {"type": "function_call_output", "call_id": "c1", "output": "real"},
    )
    audit = audit_rollout(p)
    assert len(audit.tool_calls) == 2
    assert all(not c.failed_closed for c in audit.tool_calls)
    assert audit.real_tool_output is True


def test_empty_call_id_not_closed(tmp_path: Path) -> None:
    p = _items(
        tmp_path / "r.jsonl",
        {"type": "function_call", "call_id": "", "name": "a"},
        {"type": "function_call_output", "call_id": "", "output": MARKER},
    )
    assert audit_rollout(p).real_tool_output is True


def test_orphan_output_counts(tmp_path: Path) -> None:
    p = _items(
        tmp_path / "r.jsonl", {"type": "function_call_output", "call_id": "zz", "output": "secret"}
    )
    audit = audit_rollout(p)
    assert audit.tool_calls[0].name == "<orphan>"
    assert audit.real_tool_output is True


def test_audit_call_without_output_not_closed(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "r.jsonl",
        [
            "",
            "not json",
            _line(
                "response_item",
                {"type": "custom_tool_call", "call_id": "c9", "name": "exec", "input": "x"},
            ),
        ],
    )
    audit = audit_rollout(p)
    assert audit.cwd is None and audit.model is None
    assert audit.real_tool_output is True


def test_find_rollout_across_midnight(tmp_path: Path) -> None:
    today = datetime(2026, 11, 6, 23, 59)
    dest = _place(tmp_path, today + timedelta(minutes=5))
    assert find_rollout(TID, sessions_root=tmp_path, around=today) == dest


def test_find_rollout_missing_and_ambiguous(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, 0)
    with pytest.raises(FileNotFoundError):
        find_rollout(TID, sessions_root=tmp_path, around=now)
    _place(tmp_path, now)
    d = tmp_path / "2026" / "11" / "05"
    d.mkdir(parents=True)
    shutil.copy(FIX / "toolbait_terra.jsonl", d / f"rollout-2026-11-05T01-01-01-{TID}.jsonl")
    with pytest.raises(ValueError):
        find_rollout(TID, sessions_root=tmp_path, around=now)


def test_find_rollout_ignores_other_threads(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, 0)
    _place(tmp_path, now, thread_id="01aaaaaa-0000-7000-8000-000000000009")
    with pytest.raises(FileNotFoundError):
        find_rollout(TID, sessions_root=tmp_path, around=now)


@pytest.mark.parametrize("bad", ["../../x", "", "A" * 36, "*" * 36])
def test_find_rollout_rejects_bad_thread_id(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        find_rollout(bad, sessions_root=tmp_path, around=datetime(2026, 11, 6))


def test_never_deletes_foreign(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "decoy_foreign.jsonl",
        [_line("session_meta", {"cwd": "/HOME/Documents/Codex/x"})],
    )
    assert delete_own_rollout(audit_rollout(p), own_root=tmp_path) is False
    assert p.exists()


def test_never_deletes_without_cwd(tmp_path: Path) -> None:
    p = _write(tmp_path / "nocwd.jsonl", [_line("turn_context", {"model": "m"})])
    assert delete_own_rollout(audit_rollout(p), own_root=tmp_path) is False
    assert p.exists()


def test_deletes_own(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    p = _synthetic(tmp_path / "own.jsonl", str(work), "code-mode host is disabled")
    audit = audit_rollout(p)
    assert delete_own_rollout(audit, own_root=tmp_path) is True
    assert not p.exists()
    assert delete_own_rollout(audit, own_root=tmp_path) is False


def test_refuses_relative_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    p = _write(tmp_path / "rel.jsonl", [_line("session_meta", {"cwd": "../x"})])
    assert delete_own_rollout(audit_rollout(p), own_root=tmp_path) is False
    assert p.exists()


def test_refuses_symlink_escaping_own_root(tmp_path: Path) -> None:
    own = tmp_path / "own"
    outside = tmp_path / "outside"
    own.mkdir()
    outside.mkdir()
    (own / "link").symlink_to(outside)
    p = _write(tmp_path / "sym.jsonl", [_line("session_meta", {"cwd": str(own / "link")})])
    assert delete_own_rollout(audit_rollout(p), own_root=own) is False
    assert p.exists()
