from __future__ import annotations

import json
import stat
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from promptharness.providers import codex_cli
from promptharness.providers.base import (
    AuthModeError,
    CliError,
    CliTimeout,
    CompletionRequest,
    ServedModelMismatch,
    ToolUseDetected,
    UsageLimitError,
)
from promptharness.providers.codex_cli import (
    CODEX_DISABLED_FEATURES,
    CodexCLIProvider,
    build_codex_argv,
    parse_codex_events,
)
from promptharness.providers.subprocess_runner import CliResult

FIX = Path(__file__).parents[2] / "fixtures" / "cli" / "codex"
MODEL = "gpt-6.1-terra"
LOGIN_OK = (FIX / "login_status.txt").read_text()


def _stdout(name: str) -> str:
    return (FIX / f"{name}_terra.jsonl").read_text()


def _tid(stdout: str) -> str:
    return str(json.loads(stdout.splitlines()[0])["thread_id"])


def _cli(stdout: str = "", rc: int | None = 0, **kw: Any) -> CliResult:
    base: dict[str, Any] = {
        "returncode": rc,
        "stdout": stdout,
        "stderr": "",
        "wall_ms": 123,
        "timed_out": False,
        "truncated": False,
    }
    base.update(kw)
    return CliResult(**base)


class Env:
    """Fakes run_cli for exec/version/login/delete and plants a rollout under a tmp tree."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        stdout: str,
        rollout: Path | Callable[[Path], str] | None,
        rewrite_cwd: bool = True,
        login: str = LOGIN_OK,
        delete_ok: bool = True,
    ) -> None:
        self.work_root = tmp_path / "work"
        self.sessions = tmp_path / "sessions"
        self.stdout = stdout
        self.rollout_src = rollout
        self.rewrite_cwd = rewrite_cwd
        self.login = login
        self.delete_ok = delete_ok
        self.calls: list[list[str]] = []
        self.exec_argv: list[str] = []
        self.files: dict[str, str] = {}
        self.exec_cwd: Path | None = None
        self.rollout_path: Path | None = None
        codex_cli.codex_version.cache_clear()
        monkeypatch.setattr(codex_cli, "run_cli", self._run)
        self.provider = CodexCLIProvider(work_root=self.work_root, sessions_root=self.sessions)

    def _plant(self, cwd: Path) -> None:
        if self.rollout_src is None:
            return
        tid = _tid(self.stdout)
        if callable(self.rollout_src):
            text = self.rollout_src(cwd)
        else:
            lines = []
            for ln in self.rollout_src.read_text().splitlines():
                obj = json.loads(ln)
                if self.rewrite_cwd and obj.get("type") == "session_meta":
                    obj["payload"]["cwd"] = str(cwd)
                lines.append(json.dumps(obj))
            text = "\n".join(lines) + "\n"
        now = datetime.now()
        d = self.sessions / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        d.mkdir(parents=True, exist_ok=True)
        self.rollout_path = d / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{tid}.jsonl"
        self.rollout_path.write_text(text)

    def _run(self, argv: Sequence[str], *, cwd: Path, timeout_s: float, **_: Any) -> CliResult:
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:] == ["--version"]:
            return _cli("codex-cli 9.9.9\n")
        if argv[1:] == ["login", "status"]:
            return _cli(self.login)
        if argv[1:3] == ["delete", "--force"]:
            if self.delete_ok:
                assert self.rollout_path is not None
                self.rollout_path.unlink()
                return _cli(f"Deleted session {argv[3]}.\n")
            return _cli("", rc=1)
        assert argv[1] == "exec"
        self.exec_argv = argv
        self.exec_cwd = cwd
        for opt in ("model_instructions_file=", "--output-schema"):
            for i, a in enumerate(argv):
                if opt in a:
                    path = Path(a.split("=", 1)[1].strip('"')) if "=" in a else Path(argv[i + 1])
                    self.files[opt] = path.read_text()
        self._plant(cwd)
        return _cli(self.stdout)


def _req(**kw: Any) -> CompletionRequest:
    d: dict[str, Any] = {"model": MODEL, "effort": "low", "system": "SYS", "prompt": "hi"}
    d.update(kw)
    return CompletionRequest(**d)


def _env(tmp_path: Path, mp: pytest.MonkeyPatch, name: str = "ok", **kw: Any) -> Env:
    kw.setdefault("rollout", FIX / "rollouts" / f"{name}_terra.jsonl")
    return Env(tmp_path, mp, stdout=_stdout(name), **kw)


def test_disabled_features_exact() -> None:
    assert len(CODEX_DISABLED_FEATURES) == 19
    assert CODEX_DISABLED_FEATURES[0] == "apps"
    assert CODEX_DISABLED_FEATURES[-1] == "code_mode_host"


def test_argv_disables_all_features() -> None:
    argv = build_codex_argv(
        _req(), binary="codex", instructions_file=Path("/i.md"), schema_file=None, cwd=Path("/w")
    )
    for f in CODEX_DISABLED_FEATURES:
        i = argv.index(f)
        assert argv[i - 1] == "--disable"
    assert "shell_tool" in argv and "code_mode_host" in argv
    assert not any("max_output_tokens" in a for a in argv)
    assert "--output-schema" not in argv


def test_argv_prompt_after_double_dash_and_never_cap() -> None:
    nasty = "--tools default --dangerously-bypass-approvals-and-sandbox"
    argv = build_codex_argv(
        _req(prompt=nasty, system=nasty, max_output_tokens=64),
        binary="codex",
        instructions_file=Path("/i.md"),
        schema_file=Path("/s.json"),
        cwd=Path("/w"),
    )
    assert argv[-2:] == ["--", nasty]
    assert argv.count(nasty) == 1
    assert argv[argv.index("-C") + 1] == "/w"
    assert argv[argv.index("--output-schema") + 1] == "/s.json"
    assert not any("max_output_tokens" in a for a in argv)
    assert 'model_instructions_file="/i.md"' in argv


def test_ok_fixture_maps_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch)
    c = env.provider.complete(_req())
    assert c.text == "ok"
    assert c.usage_gross.input + c.usage_gross.cache_read == 6243
    assert c.usage_gross.output == 5
    assert c.served_model == MODEL
    assert c.provider == "codex-cli"
    assert c.output_cap_applied is False
    assert c.latency_ms_api is None and c.latency_ms_wall == 123
    assert c.cli_version == "codex-cli 9.9.9"
    assert c.tool_attempts == 0
    assert env.files["model_instructions_file="] == "SYS"
    assert env.exec_cwd is not None and env.exec_cwd.parent == env.work_root


def test_quota_from_rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _env(tmp_path, monkeypatch).provider.complete(_req())
    assert c.quota is not None
    assert c.quota["primary"] == {
        "used_percent": 0.0,
        "window_minutes": 300,
        "resets_at": 1794029659,
    }
    assert c.quota["secondary"]["used_percent"] == 12.0  # type: ignore[index]


def test_quota_none_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_quota(cwd: Path) -> str:
        lines = []
        for ln in (FIX / "rollouts" / "ok_terra.jsonl").read_text().splitlines():
            obj = json.loads(ln)
            if obj["type"] == "event_msg" and obj["payload"].get("type") == "token_count":
                continue
            if obj["type"] == "session_meta":
                obj["payload"]["cwd"] = str(cwd)
            lines.append(json.dumps(obj))
        return "\n".join(lines) + "\n"

    c = _env(tmp_path, monkeypatch, rollout=no_quota).provider.complete(_req())
    assert c.quota is None


def test_schema_fixture_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, "schema")
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    c = env.provider.complete(_req(json_schema=schema))
    assert c.structured == {"answer": "ok"}
    assert json.loads(env.files["--output-schema"]) == schema


def test_schema_non_json_text_is_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, "ok")  # text "ok" is not JSON
    with pytest.raises(CliError) as ei:
        env.provider.complete(_req(json_schema={"type": "object"}))
    assert ei.value.transient is False


def test_toolbait_is_clean_or_failed_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _env(tmp_path, monkeypatch, "toolbait").provider.complete(_req())
    assert c.tool_attempts == 1
    assert c.text == "CANNOT"


def test_cap_fixture_never_applies_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, "cap")
    c = env.provider.complete(_req(max_output_tokens=64))
    assert c.output_cap_applied is False
    assert c.usage_gross.output == 1202
    assert not any("max_output_tokens" in a for a in env.exec_argv)


def _tool_rollout(output: str) -> Callable[[Path], str]:
    def make(cwd: Path) -> str:
        def ln(t: str, p: dict[str, Any]) -> str:
            return json.dumps({"type": t, "payload": p})

        return "\n".join(
            [
                ln("session_meta", {"cwd": str(cwd)}),
                ln("turn_context", {"model": MODEL, "effort": "low"}),
                ln("response_item", {"type": "function_call", "call_id": "c", "name": "sh"}),
                ln(
                    "response_item",
                    {"type": "function_call_output", "call_id": "c", "output": output},
                ),
            ]
        )

    return make


def test_real_tool_output_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, rollout=_tool_rollout("root:x:0:0"))
    with pytest.raises(ToolUseDetected):
        env.provider.complete(_req())
    assert env.rollout_path is not None and not env.rollout_path.exists()


def test_served_model_from_rollout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def other(cwd: Path) -> str:
        text = (FIX / "rollouts" / "ok_terra.jsonl").read_text().replace(MODEL, "gpt-6.1-sol")
        return text.replace("/TMP/cwd", str(cwd))

    env = _env(tmp_path, monkeypatch, rollout=other)
    with pytest.raises(ServedModelMismatch) as ei:
        env.provider.complete(_req())
    assert ei.value.served == "gpt-6.1-sol"


def test_effort_mismatch_is_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch)
    with pytest.raises(CliError) as ei:
        env.provider.complete(_req(effort="high"))
    assert ei.value.transient is False


def test_parse_skips_non_json_lines() -> None:
    ev = parse_codex_events("WARN junk\n" + _stdout("ok") + "{not json\n[1,2]\n")
    assert ev.thread_id == "01aaaaaa-0000-7000-8000-000000000001"
    assert ev.messages == ["ok"]
    assert ev.errors == []  # Code Mode error ignored
    assert ev.usage == {
        "input_tokens": 6243,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_output_tokens": 0,
    }


def _err_stdout(message: str, *, agent: bool = False) -> str:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "01aaaaaa-0000-7000-8000-000000000001"}),
        json.dumps({"type": "item.completed", "item": {"type": "error", "message": message}}),
    ]
    if agent:
        lines.append(
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "x"}})
        )
    return "\n".join(lines) + "\n"


def test_usage_limit_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(
        tmp_path,
        monkeypatch,
        stdout=_err_stdout("You've hit your usage limit. Try again in 2h"),
        rollout=None,
    )
    with pytest.raises(UsageLimitError):
        env.provider.complete(_req())


@pytest.mark.parametrize(
    ("msg", "transient"), [("stream disconnected before completion", True), ("bad thing", False)]
)
def test_other_errors_without_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, msg: str, transient: bool
) -> None:
    env = Env(tmp_path, monkeypatch, stdout=_err_stdout(msg), rollout=None)
    with pytest.raises(CliError) as ei:
        env.provider.complete(_req())
    assert ei.value.transient is transient


def test_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch)
    real = env._run

    def timed(argv: Sequence[str], **kw: Any) -> CliResult:
        if list(argv)[1] == "exec":
            return _cli("", rc=None, timed_out=True)
        return real(argv, **kw)

    monkeypatch.setattr(codex_cli, "run_cli", timed)
    with pytest.raises(CliTimeout):
        env.provider.complete(_req())


def test_auth_mode_error_and_login_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, login="Logged in using an API key\n")
    with pytest.raises(AuthModeError) as ei:
        env.provider.complete(_req())
    assert ei.value.error_class == "api_key_auth"
    assert not any(c[1] == "exec" for c in env.calls)


def test_login_status_checked_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.provider.complete(_req())
    env.rollout_path and env.rollout_path.unlink(missing_ok=True)
    env.provider.complete(_req())
    assert sum(c[1:3] == ["login", "status"] for c in env.calls) == 1


def test_own_rollout_deleted_after_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.provider.complete(_req())
    tid = _tid(env.stdout)
    assert ["codex", "delete", "--force", tid] in env.calls
    assert env.rollout_path is not None and not env.rollout_path.exists()
    assert list(env.work_root.iterdir()) == []  # workdir removed


def test_workdir_mode_0700(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch)
    modes: list[int] = []
    real = env._run

    def spy(argv: Sequence[str], *, cwd: Path, **kw: Any) -> CliResult:
        if list(argv)[1] == "exec":
            modes.append(stat.S_IMODE(cwd.stat().st_mode))
        return real(argv, cwd=cwd, **kw)

    monkeypatch.setattr(codex_cli, "run_cli", spy)
    env.provider.complete(_req())
    assert modes == [0o700]


def test_delete_failure_falls_back_to_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, monkeypatch, delete_ok=False)
    env.provider.complete(_req())
    assert any(c[1:3] == ["delete", "--force"] for c in env.calls)
    assert env.rollout_path is not None and not env.rollout_path.exists()


def test_foreign_cwd_nothing_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, rewrite_cwd=False)  # cwd stays /TMP/cwd
    env.provider.complete(_req())
    assert not any(c[1] == "delete" for c in env.calls)
    assert env.rollout_path is not None and env.rollout_path.exists()


def test_missing_rollout_is_cli_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, rollout=None)
    with pytest.raises(CliError):
        env.provider.complete(_req())
