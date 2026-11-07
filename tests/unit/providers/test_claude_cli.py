from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from promptharness.catalog import Catalog
from promptharness.cost import notional_cost_usd
from promptharness.providers import claude_cli
from promptharness.providers.base import (
    AuthModeError,
    CliError,
    CliTimeout,
    CompletionRequest,
    ServedModelMismatch,
    Status,
    ToolUseDetected,
    UsageLimitError,
)
from promptharness.providers.claude_cli import (
    ClaudeCLIProvider,
    build_claude_argv,
    parse_claude_output,
)
from promptharness.providers.subprocess_runner import CliResult

FIX = Path(__file__).parents[2] / "fixtures" / "cli" / "claude"
CATALOG = Catalog.load(Path(__file__).parents[3] / "config" / "models.yaml")
MODEL = "claude-haiku-5-5"


def _stream_lines() -> list[dict[str, Any]]:
    lines = [json.loads(ln) for ln in (FIX / "stream_init_haiku.jsonl").read_text().splitlines()]
    return [ln for ln in lines if ln]


def _result_fixture(name: str) -> dict[str, Any]:
    obj: dict[str, Any] = json.loads((FIX / name).read_text())
    return obj


def compose_stream(
    result: dict[str, Any],
    *,
    init_patch: dict[str, Any] | None = None,
    rate_limit_patch: dict[str, Any] | None = None,
    with_init: bool = True,
) -> str:
    """init line + rate_limit_event line + the json fixture's object as the result line."""
    lines = _stream_lines()
    init = next(ln for ln in lines if ln.get("type") == "system" and ln.get("subtype") == "init")
    rate = next(ln for ln in lines if ln.get("type") == "rate_limit_event")
    init = {**init, **(init_patch or {})}
    rate = {**rate, "rate_limit_info": {**rate["rate_limit_info"], **(rate_limit_patch or {})}}
    parts = ([init] if with_init else []) + [rate, result]
    return "\n".join(json.dumps(p) for p in parts) + "\n"


def cli_result(stdout: str, *, rc: int | None = 0, **kw: Any) -> CliResult:
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


class Stub:
    def __init__(self, result: CliResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.cwd_modes: list[int] = []

    def __call__(self, argv: list[str], **kw: Any) -> CliResult:
        self.calls.append({"argv": list(argv), **kw})
        self.cwd_modes.append(stat.S_IMODE(Path(kw["cwd"]).stat().st_mode))
        return self.result


@pytest.fixture(autouse=True)
def _stub_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli, "claude_version", lambda binary: "2.1.319 (Claude Code)")


def make(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: CliResult
) -> tuple[ClaudeCLIProvider, Stub]:
    stub = Stub(result)
    monkeypatch.setattr(claude_cli, "run_cli", stub)
    return ClaudeCLIProvider(work_root=tmp_path / "work"), stub


def req(**kw: Any) -> CompletionRequest:
    base: dict[str, Any] = {"model": MODEL, "effort": "low", "system": "sys", "prompt": "hello"}
    base.update(kw)
    return CompletionRequest(**base)


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def run_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str = "ok_haiku.json",
    *,
    mutate: Any = None,
    request: CompletionRequest | None = None,
    **compose_kw: Any,
) -> Any:
    obj = _result_fixture(name)
    if mutate:
        mutate(obj)
    prov, _ = make(monkeypatch, tmp_path, cli_result(compose_stream(obj, **compose_kw)))
    return prov.complete(request or req())


def test_argv_shape() -> None:
    argv = build_claude_argv(req(prompt="--bare weird", system="--tools default"))
    assert "--bare" not in argv
    assert "--fallback-model" not in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[:2] == ["claude", "-p"]
    assert argv[-2] == "--"
    assert argv[-1] == "--bare weird"
    assert argv.count("--bare weird") == 1
    before = argv[:-2]
    assert "--bare weird" not in before
    assert "--system-prompt=--tools default" in before
    assert "--tools default" not in before
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in before
    assert "--no-session-persistence" in before
    assert not any(a.startswith("--json-schema") for a in argv)
    assert argv[argv.index("--setting-sources") + 1] == ""


@pytest.mark.parametrize("prompt", ["--bare", '--settings={"x": 1}', "--model=x"])
def test_flag_like_prompt_stays_after_double_dash(prompt: str) -> None:
    argv = build_claude_argv(req(prompt=prompt))
    assert argv[-2:] == ["--", prompt]
    assert argv.count(prompt) == 1


def test_argv_schema_appended() -> None:
    argv = build_claude_argv(req(json_schema=SCHEMA, prompt="p"), binary="/x/claude")
    assert argv[0] == "/x/claude"
    assert f"--json-schema={json.dumps(SCHEMA, sort_keys=True)}" in argv[:-2]
    assert argv[-2:] == ["--", "p"]


@pytest.mark.parametrize(
    "name", ["ok_haiku.json", "schema_haiku.json", "toolbait_haiku.json", "cap_haiku.json"]
)
def test_cost_parity_all_fixtures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    obj = _result_fixture(name)
    rc = 1 if obj["is_error"] else 0
    schema = SCHEMA if "structured_output" in obj else None
    prov, _ = make(monkeypatch, tmp_path, cli_result(compose_stream(obj), rc=rc))
    c = prov.complete(req(json_schema=schema, max_output_tokens=64 if rc else None))
    assert notional_cost_usd(c.usage_gross, CATALOG.get(MODEL)) == pytest.approx(
        obj["total_cost_usd"], rel=1e-6
    )


def test_ok_fixture_maps_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    c = run_fixture(monkeypatch, tmp_path)
    assert c.served_model == "claude-haiku-5-5"
    assert c.usage_gross.cache_write_1h > 0
    assert c.status is Status.OK
    assert c.text == "ok"
    assert c.stop_reason == "end_turn"
    assert c.latency_ms_api == 762
    assert c.latency_ms_wall == 123
    assert c.cli_version == "2.1.319 (Claude Code)"
    assert c.provider == "claude-cli"
    assert c.tool_attempts == 0
    assert c.output_cap_applied is False
    assert c.cli_attempts == 1
    total = _result_fixture("ok_haiku.json")["total_cost_usd"]
    assert notional_cost_usd(c.usage_gross, CATALOG.get(MODEL)) == pytest.approx(total, rel=1e-6)


def test_quota_filled_from_rate_limit_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = run_fixture(monkeypatch, tmp_path)
    assert c.quota is not None
    assert c.quota["status"] == "allowed"
    assert c.quota["unifiedWindows"]["seven_day"]["utilization"] == 0.7  # type: ignore[index]


def test_quota_none_without_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stdout = "\n".join(
        json.dumps(x) for x in [_stream_lines()[0], _result_fixture("ok_haiku.json")]
    )
    prov, _ = make(monkeypatch, tmp_path, cli_result(stdout))
    assert prov.complete(req()).quota is None


def test_schema_fixture_structured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    c = run_fixture(monkeypatch, tmp_path, "schema_haiku.json", request=req(json_schema=SCHEMA))
    assert isinstance(c.structured["answer"], str)
    assert c.structured["answer"] == "ok"


def test_schema_two_turns_without_payload_is_tool_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def drop(o: dict[str, Any]) -> None:
        del o["structured_output"]

    with pytest.raises(ToolUseDetected):
        run_fixture(
            monkeypatch, tmp_path, "schema_haiku.json", mutate=drop, request=req(json_schema=SCHEMA)
        )


def test_schema_three_turns_is_tool_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def three(o: dict[str, Any]) -> None:
        o["num_turns"] = 3

    with pytest.raises(ToolUseDetected):
        run_fixture(
            monkeypatch,
            tmp_path,
            "schema_haiku.json",
            mutate=three,
            request=req(json_schema=SCHEMA),
        )


def test_schema_one_turn_no_payload_is_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def drop(o: dict[str, Any]) -> None:
        del o["structured_output"]
        o["num_turns"] = 1

    with pytest.raises(CliError) as ei:
        run_fixture(
            monkeypatch, tmp_path, "schema_haiku.json", mutate=drop, request=req(json_schema=SCHEMA)
        )
    assert ei.value.transient is False


def test_two_turns_allowed_only_with_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # schema fixture (2 turns + payload) but request had no schema -> tool use
    with pytest.raises(ToolUseDetected):
        run_fixture(monkeypatch, tmp_path, "schema_haiku.json")


def test_toolbait_fixture_is_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    c = run_fixture(monkeypatch, tmp_path, "toolbait_haiku.json")
    assert "CANNOT" in c.text.upper() or c.tool_attempts == 0
    assert c.usage_gross.reasoning == 40


def test_served_model_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def rekey(o: dict[str, Any]) -> None:
        o["modelUsage"] = {"claude-sonnet-5-5": o["modelUsage"][MODEL]}

    with pytest.raises(ServedModelMismatch) as ei:
        run_fixture(monkeypatch, tmp_path, mutate=rekey)
    assert ei.value.served == "claude-sonnet-5-5"


@pytest.mark.parametrize("keys", [[], [MODEL, "claude-sonnet-5-5"]])
def test_served_model_zero_or_many(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keys: list[str]
) -> None:
    def rekey(o: dict[str, Any]) -> None:
        o["modelUsage"] = {k: o["modelUsage"][MODEL] for k in keys}

    with pytest.raises(ServedModelMismatch):
        run_fixture(monkeypatch, tmp_path, mutate=rekey)


def test_multi_turn_is_tool_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def two(o: dict[str, Any]) -> None:
        o["num_turns"] = 2

    with pytest.raises(ToolUseDetected):
        run_fixture(monkeypatch, tmp_path, mutate=two)


def test_permission_denials_is_tool_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def deny(o: dict[str, Any]) -> None:
        o["permission_denials"] = [{"tool_name": "Bash"}]

    with pytest.raises(ToolUseDetected):
        run_fixture(monkeypatch, tmp_path, mutate=deny)


def test_auth_mode_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(AuthModeError) as ei:
        run_fixture(monkeypatch, tmp_path, init_patch={"apiKeySource": "/login managed key"})
    assert ei.value.error_class == "api_key_auth"


def test_init_tools_is_tool_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ToolUseDetected):
        run_fixture(monkeypatch, tmp_path, init_patch={"tools": ["Bash"]})


def test_missing_init_on_success_is_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(CliError) as ei:
        run_fixture(monkeypatch, tmp_path, with_init=False)
    assert ei.value.transient is False


def _err(result: str, **extra: Any) -> Any:
    def mutate(o: dict[str, Any]) -> None:
        o.update({"is_error": True, "result": result, "api_error_status": None, **extra})

    return mutate


def test_usage_limit_detection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(UsageLimitError):
        run_fixture(
            monkeypatch, tmp_path, mutate=_err("Claude AI usage limit reached · resets 3am")
        )


def test_usage_limit_from_rate_limit_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(UsageLimitError) as ei:
        run_fixture(
            monkeypatch,
            tmp_path,
            mutate=_err("something odd", api_error_status=400),
            rate_limit_patch={"status": "rejected", "resetsAt": 1794029400},
        )
    assert ei.value.reset_hint == "1794029400"


def test_rate_limit_not_allowed_on_success_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    c = run_fixture(monkeypatch, tmp_path, rate_limit_patch={"status": "allowed_warning"})
    assert c.quota is not None and c.quota["status"] == "allowed_warning"


def test_transient_detection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(CliError) as ei:
        run_fixture(monkeypatch, tmp_path, mutate=_err("API Error", api_error_status=529))
    assert ei.value.transient is True


def test_transient_by_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(CliError) as ei:
        run_fixture(monkeypatch, tmp_path, mutate=_err("Overloaded, try later"))
    assert ei.value.transient is True


def test_other_error_is_permanent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(CliError) as ei:
        run_fixture(monkeypatch, tmp_path, mutate=_err("bad request", api_error_status=400))
    assert ei.value.transient is False


def test_cap_hit_is_truncated_completion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    obj = _result_fixture("cap_haiku.json")
    prov, stub = make(monkeypatch, tmp_path, cli_result(compose_stream(obj), rc=1))
    c = prov.complete(req(max_output_tokens=64))
    assert c.status is Status.TRUNCATED
    assert c.text == ""
    assert c.output_cap_applied is True
    assert c.usage_gross.output == 256
    assert c.usage_gross.cache_write_1h == 1696
    assert c.served_model == MODEL
    assert stub.calls[0]["extra_env"] == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64"}


def test_no_cap_env_without_max_output_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prov, stub = make(
        monkeypatch, tmp_path, cli_result(compose_stream(_result_fixture("ok_haiku.json")))
    )
    assert prov.complete(req()).output_cap_applied is False
    assert not stub.calls[0]["extra_env"]
    assert prov.complete(req(max_output_tokens=500)).output_cap_applied is True


def test_stop_reason_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def mk(reason: str) -> Any:
        def m(o: dict[str, Any]) -> None:
            o["stop_reason"] = reason

        return m

    assert run_fixture(monkeypatch, tmp_path, mutate=mk("max_tokens")).status is Status.TRUNCATED
    assert run_fixture(monkeypatch, tmp_path, mutate=mk("refusal")).status is Status.REFUSAL
    assert run_fixture(monkeypatch, tmp_path, mutate=mk("weird")).status is Status.OK


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prov, _ = make(monkeypatch, tmp_path, cli_result("", rc=None, timed_out=True))
    with pytest.raises(CliTimeout):
        prov.complete(req())


def test_truncated_without_result_is_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prov, _ = make(monkeypatch, tmp_path, cli_result("garbage", truncated=True))
    with pytest.raises(CliError) as ei:
        prov.complete(req())
    assert ei.value.transient is False


def test_truncated_with_parsed_result_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stdout = compose_stream(_result_fixture("ok_haiku.json"))
    prov, _ = make(monkeypatch, tmp_path, cli_result(stdout, truncated=True))
    assert prov.complete(req()).text == "ok"


def test_nonzero_exit_without_error_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stdout = compose_stream(_result_fixture("ok_haiku.json"))
    prov, _ = make(monkeypatch, tmp_path, cli_result(stdout, rc=2))
    with pytest.raises(CliError):
        prov.complete(req())


def test_parse_tolerates_preamble() -> None:
    stdout = "Warning: no stdin data received in 3s\n" + compose_stream(
        _result_fixture("ok_haiku.json")
    )
    assert parse_claude_output(stdout)["result"] == "ok"


def test_parse_takes_last_result() -> None:
    a = _result_fixture("ok_haiku.json")
    b = {**a, "result": "second"}
    stdout = "\n".join(json.dumps(x) for x in (a, b))
    assert parse_claude_output(stdout)["result"] == "second"


def test_parse_no_object_raises() -> None:
    with pytest.raises(CliError) as ei:
        parse_claude_output("no json here\n")
    assert ei.value.transient is False


def test_workdir_fresh_0700_and_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prov, stub = make(
        monkeypatch, tmp_path, cli_result(compose_stream(_result_fixture("ok_haiku.json")))
    )
    prov.complete(req())
    prov.complete(req())
    assert stub.cwd_modes == [0o700, 0o700]
    cwds = [c["cwd"] for c in stub.calls]
    assert cwds[0] != cwds[1]
    assert all(Path(c).parent == tmp_path / "work" for c in cwds)
    assert list((tmp_path / "work").iterdir()) == []


def test_workdir_removed_on_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prov, _ = make(monkeypatch, tmp_path, cli_result("", rc=None, timed_out=True))
    with pytest.raises(CliTimeout):
        prov.complete(req())
    assert list((tmp_path / "work").iterdir()) == []


def test_invalid_request_rejected_before_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from promptharness.providers.base import InvalidRequestError

    prov, stub = make(monkeypatch, tmp_path, cli_result(""))
    with pytest.raises(InvalidRequestError):
        prov.complete(req(prompt="a\x00b"))
    assert stub.calls == []


def test_raw_capped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stdout = "x" * 1_500_000 + "\n" + compose_stream(_result_fixture("ok_haiku.json"))
    prov, _ = make(monkeypatch, tmp_path, cli_result(stdout))
    assert len(prov.complete(req()).raw) == 1_000_000


def test_claude_version_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.undo()  # drop the autouse stub; use the real function with a stubbed run_cli
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> CliResult:
        calls.append(argv)
        return cli_result("2.1.319 (Claude Code)\n")

    monkeypatch.setattr(claude_cli, "run_cli", fake_run)
    claude_cli.claude_version.cache_clear()
    assert claude_cli.claude_version("claude") == "2.1.319 (Claude Code)"
    assert claude_cli.claude_version("claude") == "2.1.319 (Claude Code)"
    assert calls == [["claude", "--version"]]
    claude_cli.claude_version.cache_clear()


def test_claude_version_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(claude_cli, "run_cli", lambda argv, **kw: cli_result("", rc=1))
    claude_cli.claude_version.cache_clear()
    with pytest.raises(CliError):
        claude_cli.claude_version("claude")
    claude_cli.claude_version.cache_clear()


def test_unicode_line_separators_in_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    obj = _result_fixture("ok_haiku.json")
    obj["result"] = "a\u2028b\u2029c\u0085d"
    stdout = "\n".join(
        json.dumps(x, ensure_ascii=False) for x in [_stream_lines()[0], _stream_lines()[2], obj]
    )
    assert "\u2028" in stdout
    prov, _ = make(monkeypatch, tmp_path, cli_result(stdout))
    assert prov.complete(req()).text == "a\u2028b\u2029c\u0085d"


def test_cap_hit_without_init_is_cli_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    obj = _result_fixture("cap_haiku.json")
    prov, _ = make(monkeypatch, tmp_path, cli_result(compose_stream(obj, with_init=False), rc=1))
    with pytest.raises(CliError) as ei:
        prov.complete(req(max_output_tokens=64))
    assert ei.value.transient is False


def test_usage_missing_is_cli_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def drop(o: dict[str, Any]) -> None:
        del o["usage"]

    with pytest.raises(CliError, match="usage inconsistent"):
        run_fixture(monkeypatch, tmp_path, mutate=drop)


def test_cache_creation_removed_is_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def drop(o: dict[str, Any]) -> None:
        del o["usage"]["cache_creation"]

    with pytest.raises(CliError, match="usage inconsistent"):
        run_fixture(monkeypatch, tmp_path, mutate=drop)


def test_cache_buckets_must_sum(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def bump(o: dict[str, Any]) -> None:
        o["usage"]["cache_creation_input_tokens"] += 5

    with pytest.raises(CliError, match="usage inconsistent"):
        run_fixture(monkeypatch, tmp_path, mutate=bump)


def test_init_mcp_servers_is_tool_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ToolUseDetected):
        run_fixture(monkeypatch, tmp_path, init_patch={"mcp_servers": [{"name": "x"}]})


def test_allowed_warning_529_is_transient_cli_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(CliError) as ei:
        run_fixture(
            monkeypatch,
            tmp_path,
            mutate=_err("API Error", api_error_status=529),
            rate_limit_patch={"status": "allowed_warning"},
        )
    assert type(ei.value) is CliError
    assert ei.value.transient is True


def test_regex_usage_limit_reset_hint_only_when_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(UsageLimitError) as ei:
        run_fixture(monkeypatch, tmp_path, mutate=_err("usage limit reached"))
    assert ei.value.reset_hint is None


def test_safety_incident_not_masked_by_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    obj = _result_fixture("ok_haiku.json")
    obj["num_turns"] = 3
    prov, _ = make(monkeypatch, tmp_path, cli_result(compose_stream(obj), rc=1))
    with pytest.raises(ToolUseDetected):
        prov.complete(req())


def test_claude_version_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    def boom(argv: list[str], **kw: Any) -> CliResult:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(claude_cli, "run_cli", boom)
    claude_cli.claude_version.cache_clear()
    with pytest.raises(CliError) as ei:
        claude_cli.claude_version("claude")
    assert ei.value.transient is False
    claude_cli.claude_version.cache_clear()
