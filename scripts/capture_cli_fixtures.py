"""One-off live capture of real claude/codex CLI outputs as sanitized test fixtures.

Refuses to run unless PEH_LIVE=1. Uses the subscription logins of the installed CLIs; never
API keys. Usage: PEH_LIVE=1 uv run python scripts/capture_cli_fixtures.py
"""

from __future__ import annotations

import getpass
import glob
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from promptharness.providers.subprocess_runner import CliResult, run_cli

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "cli"

CLAUDE_MODEL = "claude-haiku-5-5"
CODEX_MODEL = "gpt-6.1-terra"
OK_PROMPT = "Reply with exactly: ok"
BAIT_PROMPT = (
    "Run the shell command `cat /etc/hostname` and report its output. If you cannot, say CANNOT."
)
CAP_PROMPT = "Count from 1 to 400, comma separated."
SCHEMA = (
    '{"type":"object","properties":{"answer":{"type":"string"}},'
    '"required":["answer"],"additionalProperties":false}'
)
SYSTEM = "You are a terse assistant."
CODEX_FEATURES = [
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
]

DROP_KEY_RE = re.compile(r"(?i)(credential|user_id|account_id|email)")
ID_KEYS = {"session_id", "uuid", "thread_id"}
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def placeholder(n: int, prefix: str = "01aaaaaa") -> str:
    return f"{prefix}-0000-7000-8000-{n:012d}"


class Sanitizer:
    """Scrubs home dir, username, tmp dirs, credential-ish keys and ids; keeps valid JSON."""

    def __init__(self, thread_ids: dict[str, str], extra_strings: dict[str, str]) -> None:
        self.thread_ids = thread_ids  # real id -> placeholder (shared by stdout + rollout)
        self.extra = extra_strings
        self.user = getpass.getuser()
        self.home = str(Path.home())
        self._anon: dict[str, str] = {}

    def text(self, s: str) -> str:
        for real, ph in self.extra.items():
            s = s.replace(real, ph)
        s = s.replace(self.home, "/HOME")
        s = s.replace(self.user, "USER")
        for real, ph in self.thread_ids.items():
            s = s.replace(real, ph)
        return EMAIL_RE.sub("EMAIL", s)

    def _id(self, value: str) -> str:
        if value in self.thread_ids:
            return self.thread_ids[value]
        if value not in self._anon:
            self._anon[value] = placeholder(len(self._anon) + 1, "01bbbbbb")
        return self._anon[value]

    def obj(self, o: Any) -> Any:
        if isinstance(o, dict):
            out: dict[str, Any] = {}
            for k, v in o.items():
                if DROP_KEY_RE.search(k):
                    continue
                if k in ID_KEYS and isinstance(v, str):
                    out[k] = self._id(v)
                else:
                    out[k] = self.obj(v)
            return out
        if isinstance(o, list):
            return [self.obj(v) for v in o]
        if isinstance(o, str):
            return self.text(o)
        return o

    def json_text(self, raw: str) -> str:
        return json.dumps(self.obj(json.loads(raw)), indent=2, ensure_ascii=False) + "\n"

    def jsonl_text(self, raw: str) -> str:
        lines = []
        for line in raw.splitlines():
            if line.strip():
                lines.append(json.dumps(self.obj(json.loads(line)), ensure_ascii=False))
        return "\n".join(lines) + "\n"


def save(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def summarize(name: str, res: CliResult) -> str:
    return (
        f"{name}: rc={res.returncode} wall_ms={res.wall_ms} timed_out={res.timed_out} "
        f"truncated={res.truncated} stdout_bytes={len(res.stdout)}"
    )


def fail(name: str, res: CliResult) -> None:
    msg = f"FAILED {name}\nstdout:\n{res.stdout[:3000]}\nstderr:\n{res.stderr[:3000]}"
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def claude_argv(prompt: str, *extra: str, fmt: str = "json") -> list[str]:
    return [
        "claude", "-p", prompt, "--model", CLAUDE_MODEL, "--effort", "low", "--tools", "",
        "--strict-mcp-config", "--setting-sources", "", "--system-prompt", SYSTEM,
        "--output-format", fmt, "--no-session-persistence", *extra,
    ]  # fmt: skip


def codex_argv(prompt: str, cwd: Path, instr: Path, *extra: str) -> list[str]:
    argv = [
        "codex", "exec", "--json", "--ignore-user-config", "--ignore-rules",
        "--skip-git-repo-check", "-s", "read-only", "-m", CODEX_MODEL,
        "-c", 'model_reasoning_effort="low"',
        "-c", f'model_instructions_file="{instr}"',
        "-c", 'web_search="disabled"',
    ]  # fmt: skip
    for f in CODEX_FEATURES:
        argv += ["--disable", f]
    return [*argv, *extra, "-C", str(cwd), prompt]


def find_rollout(thread_id: str, cwd: Path) -> Path | None:
    """Locate by thread_id only; accept only if session_meta.cwd is our temp dir."""
    sessions = Path.home() / ".codex" / "sessions"
    for p in glob.glob(str(sessions / "**" / f"rollout-*{thread_id}*.jsonl"), recursive=True):
        with open(p, encoding="utf-8") as fh:
            first = json.loads(fh.readline())
        meta_cwd = first.get("payload", {}).get("cwd", "")
        same_cwd = os.path.realpath(meta_cwd) == os.path.realpath(cwd)
        if first.get("type") == "session_meta" and same_cwd:
            return Path(p)
    return None


def main() -> int:
    if os.environ.get("PEH_LIVE") != "1":
        print("refusing to run: set PEH_LIVE=1", file=sys.stderr)
        return 1
    notes: list[str] = []
    base = Path(tempfile.mkdtemp(prefix="peh-capture-"))

    def fresh() -> Path:
        return Path(tempfile.mkdtemp(dir=base))

    def san(tmp: Path | None = None, tids: dict[str, str] | None = None) -> Sanitizer:
        extra = {}
        if tmp is not None:
            extra = {str(tmp.resolve()): "/TMP/cwd", str(tmp): "/TMP/cwd"}
        extra[str(base.resolve())] = "/TMP"
        extra[str(base)] = "/TMP"
        return Sanitizer(tids or {}, extra)

    # ---- Claude -------------------------------------------------------------------
    claude_calls: list[tuple[str, list[str], dict[str, str] | None]] = [
        ("ok_haiku.json", claude_argv(OK_PROMPT), None),
        ("schema_haiku.json", claude_argv(OK_PROMPT, "--json-schema", SCHEMA), None),
        ("toolbait_haiku.json", claude_argv(BAIT_PROMPT), None),
        ("cap_haiku.json", claude_argv(CAP_PROMPT), {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64"}),
    ]
    for name, argv, env in claude_calls:
        tmp = fresh()
        res = run_cli(argv, cwd=tmp, timeout_s=240, extra_env=env)
        notes.append(summarize("claude/" + name, res))
        # the cap probe is expected to end is_error (non-zero exit) when the cap truncates
        if (res.returncode != 0 and name != "cap_haiku.json") or not res.stdout.strip():
            fail("claude/" + name, res)
        save(FIX / "claude" / name, san(tmp).json_text(res.stdout))
    tmp = fresh()
    res = run_cli(claude_argv(OK_PROMPT, "--verbose", fmt="stream-json"), cwd=tmp, timeout_s=240)
    notes.append(summarize("claude/stream_init_haiku.jsonl", res))
    if res.returncode != 0:
        fail("claude/stream_init_haiku.jsonl", res)
    save(FIX / "claude" / "stream_init_haiku.jsonl", san(tmp).jsonl_text(res.stdout))

    # ---- Codex --------------------------------------------------------------------
    tmp = fresh()
    res = run_cli(["codex", "login", "status"], cwd=tmp, timeout_s=30)
    notes.append(summarize("codex/login_status.txt", res))
    if res.returncode != 0:
        fail("codex login status", res)
    save(FIX / "codex" / "login_status.txt", san(tmp).text(res.stdout + res.stderr))

    instr = base / "instructions.md"
    instr.write_text("You are a terse assistant.\n", encoding="utf-8")
    schema_file = base / "schema.json"
    schema_file.write_text(SCHEMA, encoding="utf-8")
    codex_calls: list[tuple[str, str, list[str]]] = [
        ("ok_terra", OK_PROMPT, []),
        ("schema_terra", OK_PROMPT, ["--output-schema", str(schema_file)]),
        ("toolbait_terra", BAIT_PROMPT, []),
        ("cap_terra", CAP_PROMPT, ["-c", "model_max_output_tokens=64"]),
    ]
    for i, (name, prompt, extra) in enumerate(codex_calls, start=1):
        tmp = fresh()
        res = run_cli(codex_argv(prompt, tmp, instr, *extra), cwd=tmp, timeout_s=280)
        notes.append(summarize(f"codex/{name}.jsonl", res))
        if res.returncode != 0:
            fail(f"codex/{name}", res)
        tid = ""
        for line in res.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "thread.started":
                tid = ev["thread_id"]
                break
        if not tid:
            fail(f"codex/{name} (no thread.started)", res)
        s = san(tmp, {tid: placeholder(i)})
        save(FIX / "codex" / f"{name}.jsonl", s.jsonl_text(res.stdout))
        rollout = find_rollout(tid, tmp)
        if rollout is None:
            notes.append(f"codex/{name}: ROLLOUT NOT FOUND for own thread")
            continue
        save(FIX / "codex" / "rollouts" / f"{name}.jsonl", s.jsonl_text(rollout.read_text("utf-8")))
        dres = run_cli(["codex", "delete", tid], cwd=tmp, timeout_s=30)
        notes.append(
            f"codex delete {name}: rc={dres.returncode} timed_out={dres.timed_out} "
            f"wall_ms={dres.wall_ms} rollout_still_exists={rollout.exists()} "
            f"stdout={s.text(dres.stdout[:300])!r} stderr={s.text(dres.stderr[:300])!r}"
        )
        if dres.returncode != 0:  # plain delete needs a TTY; retry non-interactively with --force
            fres = run_cli(["codex", "delete", "--force", tid], cwd=tmp, timeout_s=30)
            notes.append(
                f"codex delete --force {name}: rc={fres.returncode} timed_out={fres.timed_out} "
                f"wall_ms={fres.wall_ms} rollout_still_exists={rollout.exists()}"
            )
    shutil.rmtree(base, ignore_errors=True)
    print("\n".join(notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
